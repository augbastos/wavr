# Wavr native desktop client

This is a native Rust and Slint viewer for Windows and Linux. It does not use a browser or WebView. It reads snapshots and device capabilities only through the `wavr_native` C ABI 1.1 library. It does not implement fusion, health, or pairing. It does not start the Core.

## Build

Install Rust and the platform C/C++ build tools, then run from this directory:

```text
cargo build --release
cargo test
```

On Windows, put `libwavr_native.dll` or `wavr_native.dll` next to the executable. On Linux, put `libwavr_native.so` there. Alternatively set `WAVR_NATIVE_LIB` to the library's path. The client checks ABI compatibility before using the snapshot interface and refuses an older library.

Start a Wavr Core separately, then run `wavr-desktop-native [--url URL] [--pin PIN]`. The default URL is `http://127.0.0.1:8000`. The client reads an optional token from `WAVR_LOCAL_TOKEN`. It polls every five seconds on a background thread. No snapshot is written to disk.

The current native library in the parent repository is ABI 1.0 and lacks the snapshot functions. Build this client now, then supply an ABI 1.1 library to run it.

## Licence

This client is AGPL-3.0-or-later, as is Wavr. Slint is used under its GPLv3 licence route; distribution of a combined binary must comply with GPLv3 obligations. See the Slint licence terms for the selected crate version before distributing binaries.

The requested system tray is omitted because this Slint integration does not expose a native tray API. Closing the window exits the viewer.
