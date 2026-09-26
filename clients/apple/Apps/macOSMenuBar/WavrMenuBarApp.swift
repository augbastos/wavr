import SwiftUI
import WavrKit

#if os(macOS)
// Standalone macOS menu bar target; do not include another @main entry.
@main
struct WavrMenuBarApp: App {
    @StateObject private var model = WavrScreenModel()

    var body: some Scene {
        MenuBarExtra("Wavr", systemImage: "house") {
            WavrStatusView(snapshot: model.snapshot, errorMessage: model.errorMessage)
                .frame(width: 380, height: 500)
                .task { await model.refresh(url: "http://127.0.0.1:8000", token: "") }
        }
        .menuBarExtraStyle(.window)
    }
}
#endif
