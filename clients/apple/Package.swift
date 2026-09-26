// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "WavrApple",
    platforms: [.iOS(.v16), .macOS(.v13), .tvOS(.v16)],
    products: [
        .library(name: "WavrKit", targets: ["WavrKit"]),
        .library(name: "WavrModels", targets: ["WavrModels"]),
    ],
    targets: [
        .systemLibrary(name: "CWavr", path: "Sources/CWavr"),
        .target(name: "WavrModels"),
        .target(name: "WavrKit", dependencies: ["CWavr", "WavrModels"]),
        .testTarget(name: "WavrKitTests", dependencies: ["WavrModels"]),
    ]
)

// WavrNative.xcframework is a binary target placeholder, not declared here because
// no artifact exists yet. Add .binaryTarget(name: "WavrNative", path: ...)
// and a WavrKit dependency after native/ exports ABI 1.1 and CI builds the slices.
