import SwiftUI
import WavrKit

@main
struct WavrTVApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                List {
                    NavigationLink("Space") {
                        WavrAmbientView(snapshot: nil)
                    }
                    NavigationLink("Rooms") {
                        WavrStatusView(snapshot: nil)
                    }
                    NavigationLink("Attention") {
                        WavrStatusView(snapshot: nil)
                    }
                }
                .navigationTitle("Wavr")
            }
        }
    }
}

struct WavrAmbientView: View {
    let snapshot: Snapshot?

    var body: some View {
        VStack(spacing: 24) {
            Text(snapshot?.runtime?.space ?? "Space unavailable")
                .font(.system(size: 76, weight: .bold))
            Text(snapshot?.runtime?.headline ?? "Waiting for Wavr")
                .font(.system(size: 42))
                .foregroundStyle(WavrTokens.dim)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(WavrTokens.background)
        .foregroundStyle(WavrTokens.text)
    }
}
