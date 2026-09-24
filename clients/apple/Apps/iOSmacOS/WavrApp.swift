import SwiftUI
import WavrKit

// Include this entry in iOS and macOS window targets only.
@main
struct WavrApp: App {
    var body: some Scene {
        WindowGroup {
            WavrStatusView(snapshot: nil)
        }
    }
}
