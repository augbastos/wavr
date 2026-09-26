# Apple validation on a Mac

Run from the repository root in a checkout containing Xcode 15 or later, its iOS and tvOS SDKs, CMake 3.20+, and the native dependency downloads (or the documented local dependency cache). Nothing in this runbook was run on Windows. Do not treat a successful model test as an app build.

## 1. Contract and pure Swift

```sh
xcode-select -p
xcodebuild -version
cmp native/include/wavr/wavr.h clients/apple/Sources/CWavr/include/wavr.h
swift build --package-path clients/apple --target WavrModels
swift build --package-path clients/apple --target WavrKit
swift build --package-path clients/apple
swift test --package-path clients/apple --filter SnapshotTests
```

PASS: `cmp` is silent with exit 0; both targets and the package compile; XCTest decodes all 12 cases from the canonical `conformance/client_view.json` and reports zero failures. The test uses `#filePath` relative to `Tests/WavrKitTests/SnapshotTests.swift` so the fixture is read from the same checkout, without a copied resource. A package build does not prove that an app links the native library.

## 2. Native Apple slices and XCFramework

The following builds five shared-library slices: macOS universal, iOS device and simulator, tvOS device and simulator. CMake may download its pinned native dependencies at configure time. Use `WAVR_JSON_HPP` and `WAVR_MBEDTLS_TARBALL` if building offline, as described in `native/CMakeLists.txt`.

```sh
cmake -S native -B build/apple-macos -G Xcode -DCMAKE_SYSTEM_NAME=Darwin -DCMAKE_OSX_ARCHITECTURES='arm64;x86_64' -DWAVR_STATIC=OFF
cmake --build build/apple-macos --config Release --target wavr_native
cmake -S native -B build/apple-ios-device -G Xcode -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_SYSROOT=iphoneos -DCMAKE_OSX_ARCHITECTURES=arm64 -DWAVR_STATIC=OFF
cmake --build build/apple-ios-device --config Release --target wavr_native
cmake -S native -B build/apple-ios-simulator -G Xcode -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_SYSROOT=iphonesimulator -DCMAKE_OSX_ARCHITECTURES='arm64;x86_64' -DWAVR_STATIC=OFF
cmake --build build/apple-ios-simulator --config Release --target wavr_native
cmake -S native -B build/apple-tvos-device -G Xcode -DCMAKE_SYSTEM_NAME=tvOS -DCMAKE_OSX_SYSROOT=appletvos -DCMAKE_OSX_ARCHITECTURES=arm64 -DWAVR_STATIC=OFF
cmake --build build/apple-tvos-device --config Release --target wavr_native
cmake -S native -B build/apple-tvos-simulator -G Xcode -DCMAKE_SYSTEM_NAME=tvOS -DCMAKE_OSX_SYSROOT=appletvsimulator -DCMAKE_OSX_ARCHITECTURES='arm64;x86_64' -DWAVR_STATIC=OFF
cmake --build build/apple-tvos-simulator --config Release --target wavr_native

lib() { find "$1" -type f -name libwavr_native.dylib -print -quit; }
mac=$(lib build/apple-macos)
ios=$(lib build/apple-ios-device)
ios_sim=$(lib build/apple-ios-simulator)
tvos=$(lib build/apple-tvos-device)
tvos_sim=$(lib build/apple-tvos-simulator)
test -n "$mac" && test -n "$ios" && test -n "$ios_sim" && test -n "$tvos" && test -n "$tvos_sim"
for binary in "$mac" "$ios" "$ios_sim" "$tvos" "$tvos_sim"; do
  file "$binary"
  nm -gU "$binary" | grep -E '_wavr_(abi_compatible|error_string|snapshot_fetch|snapshot_json|snapshot_exit_code|snapshot_free)$'
done
xcodebuild -create-xcframework \
  -library "$mac" -headers native/include/wavr \
  -library "$ios" -headers native/include/wavr \
  -library "$ios_sim" -headers native/include/wavr \
  -library "$tvos" -headers native/include/wavr \
  -library "$tvos_sim" -headers native/include/wavr \
  -output clients/apple/WavrNative.xcframework
```

PASS: all five CMake builds exit 0, all six named ABI symbols appear in every slice, and `xcodebuild -create-xcframework` exits 0 with five slices. Inspect the generated bundle with `xcrun dwarfdump --uuid clients/apple/WavrNative.xcframework` and Xcode before app integration. Do not commit the generated XCFramework without a separate packaging decision.

## 3. App targets

This checkout has no `clients/apple/WavrApple.xcodeproj`; first create the four app targets in Xcode and attach the local `WavrApple` package plus the generated XCFramework. Assign `Apps/Shared/WavrStatusView.swift` to each target. Assign only `Apps/iOSmacOS/WavrApp.swift` to the iOS and macOS window targets, only `Apps/macOSMenuBar/WavrMenuBarApp.swift` to macOS menu bar, and only `Apps/tvOS/WavrTVApp.swift` to tvOS. Each target needs exactly one `@main` source. Set iOS/tvOS deployment to 16+ and macOS to 13+. Link and embed the native framework/library according to Xcode's platform requirements, then run:

```sh
xcodebuild -project clients/apple/WavrApple.xcodeproj -scheme Wavr-iOS -configuration Debug -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
xcodebuild -project clients/apple/WavrApple.xcodeproj -scheme Wavr-macOS -configuration Debug -destination 'generic/platform=macOS' CODE_SIGNING_ALLOWED=NO build
xcodebuild -project clients/apple/WavrApple.xcodeproj -scheme Wavr-macOSMenuBar -configuration Debug -destination 'generic/platform=macOS' CODE_SIGNING_ALLOWED=NO build
xcodebuild -project clients/apple/WavrApple.xcodeproj -scheme Wavr-tvOS -configuration Debug -destination 'generic/platform=tvOS Simulator' CODE_SIGNING_ALLOWED=NO build
```

PASS: all four commands exit 0. Then launch each app on a suitable simulator or Mac: an incompatible native ABI must show the ABI error; an unanswered loopback Core must show an unknown/unavailable state and transport error; a reachable Core must show the Core's snapshot without changing its health or occupancy verdict. Check with VoiceOver and inspect app network activity for accidental external requests.

## Remaining risks

- The Xcode project, schemes, native binary dependency declaration, and app packaging are absent. The four app commands above are exact validation commands for the stated target layout, but cannot pass until those targets are created.
- Native CMake Apple compilation, dynamic library embedding/signing on iOS/tvOS, simulator architectures, and strict Swift 6 diagnostics have not been verified. The native target is explicitly `SHARED`; a static Apple packaging option cannot be selected by a CMake flag alone.
- The entry points use only loopback by default. LAN pairing and certificate pin injection need a host flow before device apps can connect to a remote Core.
