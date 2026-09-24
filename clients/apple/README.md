# Wavr Apple foundation

**BUILD UNVERIFIED — requires macOS/Xcode.** This directory was authored on Windows without a Swift toolchain. No Swift package test, CMake Apple build, XCFramework creation, simulator build, or app launch has been run.

This is a rendering client foundation for iOS, macOS, and tvOS. Wavr Core remains responsible for health, fusion, pairing, authorization, and snapshot content. The client displays values returned by the C ABI; it does not infer them. No credentials or snapshots are persisted by these sources.

## Current integration blocker

`Sources/CWavr/include/wavr.h` declares the requested ABI 1.1 snapshot API. The canonical `../../native/include/wavr/wavr.h` currently exposes ABI 1 and does not declare or implement `wavr_abi_compatible` or `wavr_snapshot_*`. The headers intentionally do **not** match yet. CI must diff the copy against the canonical header after native ABI 1.1 lands. `WavrRuntime` will reject incompatible libraries at initialization. An Apple app cannot link or fetch a snapshot from the current native runtime.

`Package.swift` contains a comment marking `WavrNative.xcframework` as a binary target placeholder. It is deliberately not declared as a live SwiftPM dependency while no binary exists. `WavrModels` and its XCTest target depend only on Foundation; `WavrKit` imports `CWavr` and requires the native binary when linked into an app.

## Build plan on a Mac

Install Xcode, its iOS and tvOS simulator runtimes, CMake, and Ninja. After native ABI 1.1 is implemented, verify the copied header:

```sh
diff -u native/include/wavr/wavr.h clients/apple/Sources/CWavr/include/wavr.h
```

From the repository root, configure each Apple slice with the Xcode Apple toolchain selected by `xcode-select`:

```sh
cmake -S native -B build/apple-ios -G Xcode -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_SYSROOT=iphoneos -DCMAKE_OSX_ARCHITECTURES=arm64 -DWAVR_STATIC=OFF
cmake --build build/apple-ios --config Release --target wavr_native
cmake -S native -B build/apple-macos -G Xcode -DCMAKE_SYSTEM_NAME=Darwin -DCMAKE_OSX_ARCHITECTURES='arm64;x86_64' -DWAVR_STATIC=OFF
cmake --build build/apple-macos --config Release --target wavr_native
cmake -S native -B build/apple-tvos -G Xcode -DCMAKE_SYSTEM_NAME=tvOS -DCMAKE_OSX_SYSROOT=appletvos -DCMAKE_OSX_ARCHITECTURES=arm64 -DWAVR_STATIC=OFF
cmake --build build/apple-tvos --config Release --target wavr_native
```

Locate each generated `libwavr_native.dylib` (Xcode generator output paths depend on CMake configuration), then create the artifact with `xcodebuild -create-xcframework` using three `-library <slice>/libwavr_native.dylib -headers native/include/wavr` pairs and `-output clients/apple/WavrNative.xcframework`. Validate the binary slices and exported ABI 1.1 symbols before adding `.binaryTarget` to `Package.swift`. The current native CMake target and dependencies may need Apple platform fixes; the commands above are a build design, not a verified recipe. Add simulator slices as needed for simulator apps.

Run pure model tests from `clients/apple` with `swift test --filter SnapshotTests`. A full app build also requires the XCFramework and a project or workspace in Xcode.

## Xcode target layout

Create separate iOS (16+), macOS (13+), macOS menu bar (13+), and tvOS (16+) SwiftUI targets. Attach the local Swift package and link/embed `WavrNative.xcframework` for each platform. Assign `Apps/Shared/WavrStatusView.swift` to all targets, `Apps/iOSmacOS/WavrApp.swift` to the iOS and macOS window targets, `Apps/macOSMenuBar/WavrMenuBarApp.swift` to the menu bar target, and `Apps/tvOS/WavrTVApp.swift` to tvOS. Each target must include exactly one `@main` app file. The tvOS entry uses remote focusable navigation and a large-type ambient screen. The shared view accepts a `Snapshot?`; app hosts must supply connection configuration and call `WavrRuntime.fetch` after explicit pairing. The nil snapshot currently shows a waiting state.

The example workflow at `ci/apple.yml.example` belongs here for review; it is not active GitHub Actions configuration. Its native/header job is expected to fail until ABI 1.1 is implemented. Simulator scheme names are placeholders until Xcode targets exist.
