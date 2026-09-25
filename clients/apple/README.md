# Wavr Apple foundation

**BUILD UNVERIFIED — requires macOS/Xcode.** These sources were audited on Windows without a Swift toolchain. No Swift build, Apple native build, XCFramework assembly, simulator build, or app launch has been run.

The Apple client renders schema 1 snapshots through the C ABI 1.1 functions (it accepts any 1.x library at 1.1 or later; the 1.2 command functions are not used yet). Core remains responsible for health, fusion, pairing, authorization, and snapshot content. Neither credentials nor snapshots are persisted by these sources. The copied header must stay byte-identical to `../../native/include/wavr/wavr.h`; `backend/tests/test_native_header_copies_are_current.py` fails when it drifts.

`WavrRuntime` checks ABI compatibility before fetching, makes blocking calls on a detached task, frees each native snapshot with `defer`, and exposes native failures to `WavrScreenModel`. The screen model only changes published UI state on the MainActor. App entries attempt the loopback Core once; a device that needs a LAN Core must provide an explicitly paired URL, token, and certificate pin to `refresh(url:token:pin:)`. The default empty token is only suitable for loopback. No LAN connection is attempted by default.

The app source layout has separate iOS/macOS window, macOS menu bar, and tvOS entries. Include exactly one `@main` entry per target and `Apps/Shared/WavrStatusView.swift` in all targets. `MenuBarExtra` is macOS-only. The pure Swift model test reads the canonical fixture through a source-relative path (`#filePath` to `../../../../conformance/client_view.json`), so SwiftPM does not carry a second copy that could drift.

## Integration status

`Package.swift` does not declare a binary target because no `WavrNative.xcframework` is checked in. This tree also has no `WavrApple.xcodeproj` or Xcode app targets. The native CMake target is a shared library; its Apple slice builds and app embedding/signing remain unverified. The example CI workflow reports these missing pieces explicitly. See [VALIDATION.md](VALIDATION.md) for the Mac build and validation commands, expected results, and remaining risks.
