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

    /** One row of the command contract (backend/wavr/client_commands.py); reply JSON
     *  {ok, status, error, detail, data}. Blocking: never call on the main thread. */
    external fun commandRun(url: String, token: String?, pin: String?, name: String,
                            argsJson: String?, timeoutMs: Int): String

    /** The certificate fingerprint a Core presents, unverified (trust on first use). */
    external fun probe(url: String, timeoutMs: Int): String
}
