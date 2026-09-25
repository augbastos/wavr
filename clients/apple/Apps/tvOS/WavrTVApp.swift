import SwiftUI
import WavrKit

#if os(tvOS)
@main
struct WavrTVApp: App {
    @StateObject private var model = WavrScreenModel()

    var body: some Scene {
        WindowGroup {
            NavigationStack {
                List {
                    NavigationLink("Space") {
                        WavrAmbientView(snapshot: model.snapshot, errorMessage: model.errorMessage)
                    }
                    NavigationLink("Rooms") {
                        WavrStatusView(snapshot: model.snapshot, errorMessage: model.errorMessage)
                    }
                    NavigationLink("Attention") {
                        WavrStatusView(snapshot: model.snapshot, errorMessage: model.errorMessage)
                    }
                }
                .navigationTitle("Wavr")
            }
            .task { await model.refresh(url: "http://127.0.0.1:8000", token: "") }
        }
    }
}
struct WavrAmbientView: View {
    let snapshot: Snapshot?
    let errorMessage: String?

    var body: some View {
        VStack(spacing: 24) {
            Text(snapshot?.runtime?.space ?? "Space unavailable")
                .font(.system(size: 76, weight: .bold))
            Text(snapshot?.runtime?.headline ?? errorMessage ?? snapshot?.error ?? "Waiting for Wavr")
                .font(.system(size: 42))
                .foregroundStyle(WavrTokens.dim)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(WavrTokens.background)
        .foregroundStyle(WavrTokens.text)
    }
}
#endif
