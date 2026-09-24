import Foundation
import CWavr
@_exported import WavrModels

public enum WavrRuntimeError: Error {
    case incompatibleABI
    case fetchFailed
    case invalidBufferSize
    case invalidJSON
}

private final class SnapshotHandle: @unchecked Sendable {
    let pointer: OpaquePointer

    init(_ pointer: OpaquePointer) { self.pointer = pointer }
    deinit { wavr_snapshot_free(pointer) }
}

private struct FetchResult: Sendable {
    let handle: SnapshotHandle
    let snapshot: Snapshot
}

public actor WavrRuntime {
    private var handle: SnapshotHandle?

    public init() throws {
        guard wavr_abi_compatible(1, 1) != 0 else {
            throw WavrRuntimeError.incompatibleABI
        }
    }

    public func fetch(
        url: String, token: String, pin: String? = nil,
        timeoutMilliseconds: Int32 = 5_000
    ) async throws -> Snapshot {
        let result = try await Task.detached(priority: .userInitiated) {
            try Self.fetchBlocking(url: url, token: token, pin: pin,
                                   timeoutMilliseconds: timeoutMilliseconds)
        }.value
        handle = result.handle
        return result.snapshot
    }

    private static func fetchBlocking(
        url: String, token: String, pin: String?, timeoutMilliseconds: Int32
    ) throws -> FetchResult {
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
        let handle = SnapshotHandle(raw)
        let needed = wavr_snapshot_json(raw, nil, 0)
        guard needed >= 0, needed < Int32.max else {
            throw WavrRuntimeError.invalidBufferSize
        }
        var bytes = [CChar](repeating: 0, count: Int(needed) + 1)
        let written = bytes.withUnsafeMutableBufferPointer { buffer in
            wavr_snapshot_json(raw, buffer.baseAddress, buffer.count)
        }
        guard written >= 0, written <= needed else {
            throw WavrRuntimeError.invalidBufferSize
        }
        let data = bytes.withUnsafeBytes { buffer in
            Data(bytes: buffer.baseAddress!, count: Int(written))
        }
        guard let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else {
            throw WavrRuntimeError.invalidJSON
        }
        return FetchResult(handle: handle, snapshot: snapshot)
    }
}
