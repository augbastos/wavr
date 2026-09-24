import SwiftUI
import WavrKit

public struct WavrStatusView: View {
    public let snapshot: Snapshot?

    public init(snapshot: Snapshot?) { self.snapshot = snapshot }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(snapshot?.runtime?.space ?? "Space unavailable")
                        .font(.largeTitle.bold())
                    Text(snapshot?.runtime?.headline ?? snapshot?.error ?? "Waiting for Wavr")
                        .foregroundStyle(WavrTokens.dim)
                    HStack {
                        Circle()
                            .fill(WavrTokens.color(for: snapshot?.runtime?.state))
                            .frame(width: 12, height: 12)
                        Text(snapshot?.runtime?.state?.rawValue.capitalized ?? "Unknown")
                    }
                    .accessibilityElement(children: .combine)
                }

                if snapshot?.roomsReadable == true, let rooms = snapshot?.rooms {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Rooms").font(.title2.bold())
                        ForEach(Array(rooms.enumerated()), id: \.offset) { entry in
                            HStack {
                                Text(entry.element.room ?? "Unnamed room")
                                Spacer()
                                Text(entry.element.occupied.map { $0 ? "Occupied" : "Empty" } ?? "Unknown")
                                    .foregroundStyle(WavrTokens.dim)
                            }
                            .padding()
                            .background(WavrTokens.surface)
                            .clipShape(RoundedRectangle(cornerRadius: 12))
                        }
                    }
                } else {
                    Text("Rooms could not be read")
                        .foregroundStyle(WavrTokens.dim)
                }

                if let attention = snapshot?.attention {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Attention").font(.title2.bold())
                        Text(attention.headline ?? "No attention summary")
                        ForEach(Array((attention.items ?? []).enumerated()), id: \.offset) { entry in
                            Text(entry.element.title ?? "Untitled item")
                                .padding()
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(WavrTokens.elevated)
                                .clipShape(RoundedRectangle(cornerRadius: 12))
                        }
                    }
                }
            }
            .frame(maxWidth: 760, alignment: .leading)
            .padding(24)
        }
        .background(WavrTokens.background)
        .foregroundStyle(WavrTokens.text)
    }
}
