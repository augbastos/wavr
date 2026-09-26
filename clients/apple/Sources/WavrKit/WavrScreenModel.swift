import Foundation
import Combine
import WavrModels

@MainActor
public final class WavrScreenModel: ObservableObject {
    @Published public private(set) var snapshot: Snapshot?
    @Published public private(set) var errorMessage: String?
    private let runtime: WavrRuntime?

    public init() {
        do {
            runtime = try WavrRuntime()
        } catch {
            runtime = nil
            errorMessage = error.localizedDescription
        }
    }

    public func refresh(url: String, token: String, pin: String? = nil) async {
        guard let runtime else { return }
        do {
            snapshot = try await runtime.fetch(url: url, token: token, pin: pin)
            errorMessage = nil
        } catch {
            snapshot = nil
            errorMessage = error.localizedDescription
        }
    }
}
