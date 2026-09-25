import Foundation
import CWavr
@_exported import WavrModels

public enum WavrRuntimeError: Error, LocalizedError, Sendable {
    case incompatibleABI
    case fetchFailed
    case native(code: Int32, message: String)
    case invalidBufferSize
    case invalidJSON
    case invalidExitCode
    case embeddedNUL

    public var errorDescription: String? {
        switch self {
        case .incompatibleABI: return "Wavr native library is incompatible with ABI 1.1."
        case .fetchFailed: return "Wavr native library could not create a snapshot."
        case let .native(code, message): return "Wavr native error \(code): \(message)"
        case .invalidBufferSize: return "Wavr native library returned an invalid JSON buffer size."
        case .invalidJSON: return "Wavr native library returned an invalid snapshot."
        case .invalidExitCode: return "Wavr native library returned an invalid snapshot exit code."
        case .embeddedNUL: return "Wavr connection parameter contains a NUL byte."
        }
    }
}

public actor WavrRuntime {
    private static let maximumJSONBytes = 16_777_216

    public init() throws {
        // A literal, not the header's constants: this client calls 1.1 functions
        // only, and must not demand a newer library just because the shared
        // header moved on (the desktop binding does the same).
        guard wavr_abi_compatible(1, 1) == 1 else {
            throw WavrRuntimeError.incompatibleABI
        }
    }

    public func fetch(
        url: String, token: String, pin: String? = nil,
        timeoutMilliseconds: Int32 = 5_000
    ) async throws -> Snapshot {
        // The C ABI blocks for up to three requests. Never run it on the UI executor.
        return try await Task.detached(priority: .userInitiated) {
            try Self.fetchBlocking(url: url, token: token, pin: pin,
                                   timeoutMilliseconds: timeoutMilliseconds)
        }.value
    }

    private static func nativeError(_ code: Int32) -> WavrRuntimeError {
        let message = wavr_error_string(code).map { String(cString: $0) } ?? "Unknown native error"
        return .native(code: code, message: message)
    }

    private static func fetchBlocking(
        url: String, token: String, pin: String?, timeoutMilliseconds: Int32
    ) throws -> Snapshot {
        guard !url.contains("\0"), !token.contains("\0"), pin?.contains("\0") != true else {
            throw WavrRuntimeError.embeddedNUL
        }
        let raw = url.withCString { urlPointer in
            token.withCString { tokenPointer in
                if let pin {
                    return pin.withCString { pinPointer in
                        wavr_snapshot_fetch(urlPointer, tokenPointer, pinPointer,
                                            timeoutMilliseconds)
                    }
                }
                return wavr_snapshot_fetch(urlPointer, tokenPointer, nil,
                                           timeoutMilliseconds)
            }
        }
        guard let raw else { throw WavrRuntimeError.fetchFailed }
        defer { wavr_snapshot_free(raw) }

        let json = try readJSON(from: raw)
        let code = wavr_snapshot_exit_code(raw)
        if code < 0 { throw nativeError(code) }
        guard (0...2).contains(code) else { throw WavrRuntimeError.invalidExitCode }
        guard let snapshot = try? JSONDecoder().decode(Snapshot.self, from: json),
              snapshot.schema == 1, snapshot.exitCode == Int(code) else {
            throw WavrRuntimeError.invalidJSON
        }
        return snapshot
    }

    private static func readJSON(from raw: OpaquePointer) throws -> Data {
        var needed = wavr_snapshot_json(raw, nil, 0)
        if needed < 0 { throw nativeError(needed) }
        while true {
            guard needed >= 0, Int(needed) <= maximumJSONBytes else {
                throw WavrRuntimeError.invalidBufferSize
            }
            var bytes = [CChar](repeating: 0, count: Int(needed) + 1)
            let written = bytes.withUnsafeMutableBufferPointer { buffer in
                wavr_snapshot_json(raw, buffer.baseAddress, buffer.count)
            }
            if written < 0 { throw nativeError(written) }
            // A too-small buffer returns the required size, excluding the NUL.
            if Int(written) >= bytes.count {
                needed = written
                continue
            }
            guard bytes[Int(written)] == 0 else { throw WavrRuntimeError.invalidJSON }
            return bytes.withUnsafeBytes { buffer in
                Data(bytes: buffer.baseAddress!, count: Int(written))
            }
        }
    }
}
