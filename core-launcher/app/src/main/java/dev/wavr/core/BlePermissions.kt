package dev.wavr.core

import android.Manifest
import android.bluetooth.BluetoothManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * The Android 12+ Bluetooth permission model, stated in one place.
 *
 * WHAT IS AND IS NOT BUILT — say it here so nobody has to guess from the
 * manifest. The Core's BLE presence source is Python (`[ble]` extra, `bleak`),
 * and `bleak` has no working Android backend under Chaquopy: it needs the
 * platform's `BluetoothLeScanner`, which only Kotlin can reach. So a BLE **node
 * scanner** for Android is designed here and NOT implemented. What exists is the
 * permission model, the state read, and the request plumbing, so the scanner
 * lands on a surface that already tells the truth about what it needs.
 *
 * WHY `neverForLocation` IS **NOT** CLAIMED, and this is the deliberate part.
 * `usesPermissionFlags="neverForLocation"` lets an app take `BLUETOOTH_SCAN` on
 * API 31+ without `ACCESS_FINE_LOCATION`, and it is an assertion: *I do not use
 * scan results to derive physical location.* Wavr's BLE source exists precisely
 * to infer **which room a person is in** from RSSI. Whatever one thinks of the
 * spirit of the flag, that is location derivation, and asserting otherwise to
 * dodge a permission prompt would be a lie in a manifest — in the same app whose
 * Data Safety story is its product. So:
 *
 *  - `BLUETOOTH_SCAN` is declared WITHOUT the flag, which means
 *  - `ACCESS_FINE_LOCATION` is genuinely required on every API level, and
 *  - therefore **neither is declared in the manifest yet**, because declaring a
 *    location permission for a scanner that does not exist is worse than the
 *    prompt it avoids.
 *
 * The moment the scanner is written, both go in together with a rationale
 * screen. If the scanner is ever narrowed to house-level presence only (device
 * seen / not seen, no room), then `neverForLocation` becomes the honest claim and
 * `ACCESS_FINE_LOCATION` can be dropped — that is a real, cheaper design and it
 * is on the table.
 *
 * What IS declared today is `BLUETOOTH_CONNECT` (API 31+) and legacy `BLUETOOTH`
 * (`maxSdkVersion=30`), both inherited from the existing kiosk, and neither is
 * requested at runtime: reading whether the adapter is switched on needs no
 * grant, and a kiosk that cannot read it just reports `bt:false`.
 */
object BlePermissions {

    /** Runtime permissions a BLE *scan* would need on this API level. */
    fun requiredForScan(): List<String> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            // API 31+: the new, scoped pair. FINE_LOCATION is still required
            // because we do NOT assert neverForLocation (see the class note).
            listOf(
                Manifest.permission.BLUETOOTH_SCAN,
                Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.ACCESS_FINE_LOCATION,
            )
        } else {
            // API 26..30: BLUETOOTH / BLUETOOTH_ADMIN are install-time; the
            // runtime gate on a scan is location, and it always was.
            listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        }

    /** Runtime permissions needed merely to read adapter state: none. */
    fun requiredForAdapterState(): List<String> = emptyList()

    fun granted(context: Context, permission: String): Boolean = try {
        ContextCompat.checkSelfPermission(context, permission) ==
            PackageManager.PERMISSION_GRANTED
    } catch (t: Throwable) {
        false
    }

    /** True when every permission a scan needs is already held. */
    fun scanReady(context: Context): Boolean =
        requiredForScan().all { granted(context, it) }

    /** Whether the device even has BLE hardware. Tristate, per §5.1. */
    fun hasBle(context: Context): Boolean? = try {
        context.packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH_LE)
    } catch (t: Throwable) {
        null
    }

    /** Adapter switched on? Needs no grant; a throw degrades to false. */
    fun adapterEnabled(context: Context): Boolean = try {
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)
            ?.adapter?.isEnabled == true
    } catch (t: Throwable) {
        false
    }

    /**
     * `{"hasBle":..,"adapterOn":..,"scanReady":..,"scannerImplemented":false,
     *   "missing":[..],"note":".."}`
     *
     * `scannerImplemented` is hard-coded false on purpose. When someone writes
     * the scanner they will have to come here and flip it, which is exactly the
     * right amount of friction for a claim about what the app can sense.
     */
    fun state(context: Context): String = try {
        val missing = JSONArray()
        requiredForScan().forEach { if (!granted(context, it)) missing.put(it) }
        JSONObject().apply {
            put("hasBle", hasBle(context) ?: JSONObject.NULL)
            put("adapterOn", adapterEnabled(context))
            put("scanReady", missing.length() == 0)
            put("scannerImplemented", false)
            put("missing", missing)
            put(
                "note",
                "Wavr does not scan for Bluetooth devices on this phone yet. " +
                    "When it does, it will ask for location permission first and " +
                    "explain that BLE signal strength is how it works out which " +
                    "room you are in."
            )
        }.toString()
    } catch (t: Throwable) {
        "{\"hasBle\":null,\"adapterOn\":false,\"scanReady\":false,\"scannerImplemented\":false}"
    }
}
