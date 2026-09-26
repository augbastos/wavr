import SwiftUI
import WavrKit

#if os(iOS) || os(macOS)
// Include this entry in iOS and macOS window targets only.
@main
struct WavrApp: App {
    @StateObject private var model = WavrScreenModel()

    var body: some Scene {
        WindowGroup {
            WavrStatusView(snapshot: model.snapshot, errorMessage: model.errorMessage)
                .task { await model.refresh(url: "http://127.0.0.1:8000", token: "") }
        }
    }
}
#endif
