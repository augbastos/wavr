package dev.wavr.core.nativeui

/** JNI bridge supplied by the separately packaged wavr_native_jni library. */
object WavrNative {
    val available: Boolean = try {
        System.loadLibrary("wavr_native_jni")
        true
    } catch (_: UnsatisfiedLinkError) {
        false
    } catch (_: SecurityException) {
        false
    }

    external fun abiCompatible(major: Int, minor: Int): Boolean
    external fun snapshotFetch(url: String, credential: String?, pin: String?, timeoutMs: Int): String
    external fun capabilityManifest(): String
}
