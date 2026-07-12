package dev.wavr.mobile.wavrhealth

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin

/**
 * WavrHealth — LOCAL transport classifier for the shim's health screen (CONTRACT, v1).
 * A sibling of WavrNet; the WavrNet contract stays FROZEN.
 *
 * JS registration (the shim): const WavrHealth = Capacitor.registerPlugin("WavrHealth");
 * Contract:                    see definitions/wavr-health.d.ts (frozen; iOS-neutral).
 *
 * DELIBERATELY MINIMAL: exactly one method, networkInfo(). Hub reachability + the
 * TLS/pin leg is WavrNet.probe's job — this plugin adds NO second reachability path
 * (that would duplicate the single network choke point). Its only role is to tell the
 * health screen whether THIS device is on Wi-Fi / cellular / offline so an unreachable
 * hub can be explained honestly.
 *
 * PRIVACY: reads ConnectivityManager transport capabilities under the already-declared
 * ACCESS_NETWORK_STATE. It does NOT read SSID/BSSID and does NOT use location — the
 * transport type needs neither. No network I/O. Logs nothing.
 */
@CapacitorPlugin(name = "WavrHealth")
class WavrHealthPlugin : Plugin() {

    /**
     * -> {transport, online, metered}. The active local transport of this device.
     * Never rejects; degrades to {transport:'none', online:false, metered:false} if
     * connectivity state is unreadable.
     */
    @PluginMethod
    fun networkInfo(call: PluginCall) {
        val ret = JSObject()
        ret.put("transport", "none")
        ret.put("online", false)
        ret.put("metered", false)
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            val active = cm?.activeNetwork
            val caps = active?.let { cm.getNetworkCapabilities(it) }
            if (caps != null) {
                ret.put("transport", transportOf(caps))
                ret.put(
                    "online",
                    caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
                        caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                )
                // isActiveNetworkMetered honors the user's "treat as metered" override too.
                ret.put("metered", cm.isActiveNetworkMetered)
            }
        } catch (_: Exception) {
            // Never fail the health screen on a connectivity read: the defaults above stand.
        }
        call.resolve(ret)
    }

    private fun transportOf(caps: NetworkCapabilities): String = when {
        caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
        caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
        caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
        caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) -> "vpn"
        else -> "other"
    }
}
