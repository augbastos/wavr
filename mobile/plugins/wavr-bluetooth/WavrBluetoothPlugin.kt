package dev.wavr.mobile.wavrbluetooth

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.content.Context
import android.os.Build
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import com.getcapacitor.annotation.Permission
import com.getcapacitor.annotation.PermissionCallback

/**
 * WavrBluetooth — READ-ONLY bridge to THIS phone's bonded Bluetooth devices
 * (CONTRACT, v1). A sibling of WavrNet; the WavrNet contract stays FROZEN.
 *
 * JS registration (the shim): const WavrBluetooth = Capacitor.registerPlugin("WavrBluetooth");
 * Contract:                    see definitions/wavr-bluetooth.d.ts (frozen; iOS-neutral).
 *
 * WHAT IT DOES: returns BluetoothAdapter.bondedDevices — the user's OWN, already
 * paired hardware — as {address, name} pairs, so the admin phone can register those
 * MACs as labels in the Core's identity registry (POST /api/identity/devices).
 *
 * WHAT IT DELIBERATELY DOES NOT DO (the whole privacy point):
 *  - No startDiscovery(), no BLE scan, no connect: it never touches a device that
 *    the user has not already bonded. Reading bonded peers is not "following
 *    strangers' devices" (ADR-004) — it is enrichment of the user's own registry.
 *  - Requests ONLY BLUETOOTH_CONNECT (API 31+). NO BLUETOOTH_SCAN, NO location —
 *    the bonded read needs neither, and not declaring them keeps the Play
 *    Data-Safety form honestly "no data collected". Below API 31 the legacy
 *    install-time BLUETOOTH permission (maxSdkVersion=30) covers the read with no
 *    runtime prompt.
 *
 * LOGGING: this file logs nothing — never a MAC, never a device name.
 */
@CapacitorPlugin(
    name = "WavrBluetooth",
    permissions = [
        Permission(alias = "bluetooth", strings = [Manifest.permission.BLUETOOTH_CONNECT]),
    ]
)
class WavrBluetoothPlugin : Plugin() {

    /**
     * -> {devices:[{address,name}]}. The bonded peers of this phone. Rejects:
     * 'NO_ADAPTER' (no Bluetooth hardware), 'BT_OFF' (adapter disabled),
     * 'PERMISSION_DENIED' (API 31+ without BLUETOOTH_CONNECT).
     */
    @PluginMethod
    fun listBonded(call: PluginCall) {
        val adapter = adapterOrNull()
        if (adapter == null) {
            call.reject("this device has no Bluetooth adapter", "NO_ADAPTER")
            return
        }
        // API 31+: reading bondedDevices / a device's name requires BLUETOOTH_CONNECT.
        // Below 31 the legacy install-time BLUETOOTH permission already covers it.
        if (Build.VERSION.SDK_INT >= 31 && getPermissionState("bluetooth").toString() != "granted") {
            call.reject("BLUETOOTH_CONNECT is required (call requestPermissions first)", "PERMISSION_DENIED")
            return
        }
        if (!adapter.isEnabled) {
            call.reject("Bluetooth is turned off", "BT_OFF")
            return
        }

        val devices = JSArray()
        try {
            // bondedDevices is the ALREADY-paired set — no scan, no discovery.
            for (device in adapter.bondedDevices ?: emptySet()) {
                val addr = device.address ?: continue          // real bonded MAC (not the local-adapter redaction)
                val entry = JSObject()
                entry.put("address", addr.uppercase())
                // name can be null while the platform withholds it; a label only, "" then.
                entry.put("name", device.name ?: "")
                devices.put(entry)
            }
        } catch (e: SecurityException) {
            // Permission revoked between the check above and the read.
            call.reject("BLUETOOTH_CONNECT is required (call requestPermissions first)", "PERMISSION_DENIED")
            return
        }
        val ret = JSObject()
        ret.put("devices", devices)
        call.resolve(ret)
    }

    // ---- permissions -----------------------------------------------------------------

    /** -> {bluetooth}. 'na' below API 31 (no runtime prompt there). Never prompts. */
    @PluginMethod
    override fun checkPermissions(call: PluginCall) {
        call.resolve(permissionsSnapshot())
    }

    /**
     * Prompt for BLUETOOTH_CONNECT on Android 12+ only; below API 31 the read needs
     * no runtime grant, so this resolves the current snapshot unchanged.
     */
    @PluginMethod
    override fun requestPermissions(call: PluginCall) {
        if (Build.VERSION.SDK_INT >= 31) {
            requestPermissionForAlias("bluetooth", call, "permissionsCallback")
        } else {
            call.resolve(permissionsSnapshot())
        }
    }

    @PermissionCallback
    private fun permissionsCallback(call: PluginCall) {
        call.resolve(permissionsSnapshot())
    }

    private fun permissionsSnapshot(): JSObject {
        val ret = JSObject()
        ret.put(
            "bluetooth",
            if (Build.VERSION.SDK_INT >= 31) getPermissionState("bluetooth").toString() else "na"
        )
        return ret
    }

    private fun adapterOrNull(): BluetoothAdapter? =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
}
