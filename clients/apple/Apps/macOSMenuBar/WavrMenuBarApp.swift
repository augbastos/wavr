import SwiftUI
import WavrKit

// Standalone macOS menu bar target; do not include another @main entry.
@main
struct WavrMenuBarApp: App {
    var body: some Scene {
        MenuBarExtra("Wavr", systemImage: "house") {
            WavrStatusView(snapshot: nil)
                .frame(width: 380, height: 500)
        }
        .menuBarExtraStyle(.window)
    }
}
