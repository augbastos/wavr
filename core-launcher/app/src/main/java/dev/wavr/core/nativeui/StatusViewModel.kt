package dev.wavr.core.nativeui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

data class StatusUiState(
    val snapshot: Snapshot? = null,
    val manifest: DeviceManifest? = null,
    val message: String? = null
)

class StatusViewModel(app: Application) : AndroidViewModel(app) {
    private val store = ConnectionStore(app)
    private val mutableState = MutableStateFlow(StatusUiState())
    val state = mutableState.asStateFlow()
    private var polling: Job? = null
    private var connection: Connection? = null   // resolved once; again after a miss

    fun start() {
        if (polling?.isActive == true) return
        if (!WavrNative.available) {
            mutableState.value = StatusUiState(message = "Native runtime is not bundled")
            return
        }
        polling = viewModelScope.launch {
            while (true) {
                try {
                    val next = withContext(Dispatchers.IO) {
                        // 1.2: the command contract (Manage) is part of what this app calls.
                        if (!WavrNative.abiCompatible(1, 2)) {
                            StatusUiState(message = "Native runtime ABI is incompatible")
                        } else {
                            // The joined Core (pinned, with its token), else a Core on this device.
                            val c = connection ?: store.load().also { connection = it }
                            val snap = Snapshot.parse(WavrNative.snapshotFetch(c.url, c.token, c.pin, 6000))
                            if (snap.reachable != true) connection = null
                            StatusUiState(
                                snapshot = snap,
                                manifest = DeviceManifest.parse(WavrNative.capabilityManifest())
                            )
                        }
                    }
                    mutableState.value = next
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    mutableState.value = StatusUiState(message = "Native snapshot could not be read")
                } catch (_: LinkageError) {
                    mutableState.value = StatusUiState(message = "Native runtime is not bundled")
                }
                delay(5_000)
            }
        }
    }

    fun stop() {
        polling?.cancel()
        polling = null
    }
}
