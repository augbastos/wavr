# Physical Android TV UI smoke check

This uses the existing `native/tests/android_ui_smoke.py` test. It starts a
temporary loopback Core on the host, enables simulated rooms, uses `adb reverse`
to expose that Core to the TV, installs a debug APK and checks both phone and
TV activities. It does not use household data. Run only on a TV you own and
may install on.

From the repository root:

```sh
adb devices
python native/tests/android_ui_smoke.py --serial TV_ADB_SERIAL \
  --apk core-launcher/app/build/outputs/apk/debug/app-debug.apk \
  --out android-tv-shots
```

Build the debug APK first if absent:

```sh
cd core-launcher
./gradlew :app:assembleDebug
cd ..
```

**PASS** is exit code 0 and the final `passed` verdict: the APK installs, the
native runtime loads, the Core's state and at least one room appear in the
phone activity, the TV status renders, and D-pad input moves TV focus. Review
the saved `tv-status.png` and `tv-after-dpad.png` screenshots for visible
clipping or focus defects. A missing TV, adb connection, APK or screenshot
review is **CANNOT TEST**, not a physical TV PASS. The existing script was
previously exercised on an emulator, not a physical TV.
