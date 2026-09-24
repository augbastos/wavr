package dev.wavr.core.nativeui

import androidx.lifecycle.ViewModel
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

class StatusViewModel : ViewModel() {
    private val mutableState = MutableStateFlow(StatusUiState())
    val state = mutableState.asStateFlow()
    private var polling: Job? = null

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
                        if (!WavrNative.abiCompatible(1, 0)) {
                            StatusUiState(message = "Native runtime ABI is incompatible")
                        } else {
                            StatusUiState(
                                snapshot = Snapshot.parse(WavrNative.snapshotFetch(
                                    "http://127.0.0.1:8000", null, null, 6000)),
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
