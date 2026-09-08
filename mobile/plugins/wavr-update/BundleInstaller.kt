package dev.wavr.mobile.wavrupdate

import android.content.Context
import android.util.Log
import dev.wavr.mobile.securestorage.SecureKeyStore
import dev.wavr.mobile.wavrnet.PinMismatchException
import dev.wavr.mobile.wavrnet.PinnedClient
import java.io.File
import java.io.IOException
import java.net.URI
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPInputStream
import javax.net.ssl.SSLPeerUnverifiedException
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * BundleInstaller — downloads, verifies, and SAFE-UNTARS a WEB-ASSET OTA bundle.
 *
 * It reuses the app's ONE trust anchor and secret store and adds nothing:
 *  - TRANSPORT: PinnedClient — the SAME pinned-TLS OkHttp factory WavrNet uses,
 *    trusting EXACTLY the one SHA-256 fingerprint captured at pairing. No second HTTP
 *    stack, no unpinned fetch. A cert change fails closed (PIN_MISMATCH + presentedFp).
 *  - SECRETS: {centralUrl, pinnedFp, token} are read from SecureKeyStore natively; the
 *    Bearer token NEVER crosses the JS bridge and is NEVER logged.
 *  - PEER: the bundle URL's host:port MUST equal the stored central's — the app's only
 *    permitted network peer. Any other host is refused.
 *
 * INTEGRITY ORDER (nothing is unpacked from unverified bytes):
 *   download -> size-cap -> SHA-256 over the .tar.gz -> compare to the manifest hash
 *   -> ONLY THEN gunzip + SafeTarExtractor into a staging dir -> atomic rename into
 *   the version dir. Any failure deletes the temp + staging and leaves prior state.
 *
 * LOGGING (logcat is hostile): host:port and machine codes only — never the token,
 * the full URL, or the payload.
 */
class BundleInstaller(private val context: Context) {

    private val store = SecureKeyStore(context)

    data class Opts(val url: String, val sha256: String, val size: Long, val version: String)

    /** Install [opts] and return the staged bundle's directory path. Throws BundleException. */
    fun install(opts: Opts): String {
        val central = store.get(K_URL)
        val fp = store.get(K_FP)
        val token = store.get(K_TOKEN)
        if (central.isNullOrBlank() || fp.isNullOrBlank() || token.isNullOrBlank()) {
            throw BundleException("NOT_PAIRED", "no paired central")
        }
        if (!opts.url.startsWith("https://", ignoreCase = true)) {
            throw BundleException("INVALID_ARGS", "bundle URL must be https")
        }
        // SINGLE-PEER egress invariant: the bundle host:port must be the paired central.
        if (!sameHostPort(opts.url, central)) {
            throw BundleException("INVALID_ARGS", "bundle host is not the paired central")
        }
        if (opts.size <= 0 || opts.size > MAX_DOWNLOAD) {
            throw BundleException("SIZE_EXCEEDED", "declared size is out of bounds")
        }
        val expected = hexToBytes(PinnedClient.normalizeFp(opts.sha256).lowercase())
            ?: throw BundleException("INVALID_ARGS", "sha256 is not valid hex")

        val client = try {
            downloadClient(fp)
        } catch (_: IllegalArgumentException) {
            throw BundleException("NOT_PAIRED", "stored fingerprint is unusable")
        }

        val root = File(context.filesDir, WEB_DIR).apply { mkdirs() }
        val tmp = File(root, ".dl-${opts.version}.tgz")
        val staging = File(root, ".staging-${opts.version}")
        val finalDir = File(root, opts.version)
        deleteRecursively(staging)
        tmp.delete()

        try {
            downloadAndHash(client, opts.url, token, opts.size, expected, tmp)
            staging.mkdirs()
            GZIPInputStream(tmp.inputStream().buffered()).use { gz ->
                SafeTarExtractor.extract(gz, staging)      // fail-closed safe untar
            }
            deleteRecursively(finalDir)
            if (!staging.renameTo(finalDir)) {
                throw BundleException("STORAGE", "could not activate the staged bundle")
            }
            return finalDir.path
        } catch (e: BundleException) {
            throw e
        } catch (e: Exception) {
            throw classify(opts.url, e)
        } finally {
            tmp.delete()
            deleteRecursively(staging)                     // no-op after a successful rename
        }
    }

    // ---- download + streaming hash ----------------------------------------------------

    private fun downloadAndHash(
        client: OkHttpClient, url: String, token: String, cap: Long, expected: ByteArray, tmp: File
    ) {
        val request = Request.Builder()
            .url(url)
            .header("Authorization", "Bearer $token")      // never logged
            .header("Accept", "application/gzip")
            .get()
            .build()
        client.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw BundleException("HTTP_${resp.code}", "central returned a non-2xx status")
            }
            val body = resp.body ?: throw BundleException("NETWORK", "empty response body")
            val digest = MessageDigest.getInstance("SHA-256")
            var total = 0L
            body.byteStream().use { input ->
                tmp.outputStream().buffered().use { out ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        total += n
                        if (total > cap) {                 // zip-bomb / oversize guard
                            throw BundleException("SIZE_EXCEEDED", "download exceeded the size cap")
                        }
                        digest.update(buf, 0, n)
                        out.write(buf, 0, n)
                    }
                }
            }
            if (total != cap) {
                throw BundleException("HASH_MISMATCH", "downloaded size did not match the manifest")
            }
            if (!MessageDigest.isEqual(expected, digest.digest())) {   // constant-time
                throw BundleException("HASH_MISMATCH", "SHA-256 did not match the manifest")
            }
        }
    }

    /** Pin-preserving client with a bounded whole-call timeout for a larger download. */
    private fun downloadClient(fp: String): OkHttpClient =
        PinnedClient.clientFor(fp)                          // THE trust anchor — throws on bad fp
            .newBuilder()                                   // copies pinned TrustManager + verifier
            .callTimeout(DOWNLOAD_TIMEOUT_S, TimeUnit.SECONDS)
            .readTimeout(READ_TIMEOUT_S, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .build()

    // ---- error classification (mirrors WavrNet/SensorStreamService) -------------------

    private fun classify(url: String, t: Throwable): BundleException {
        if (isPinFailure(t)) {
            Log.w(TAG, "TLS pin mismatch for ${redactedHost(url)} — OTA fail-closed")
            return BundleException("PIN_MISMATCH", "certificate changed", safeProbeFp(url))
        }
        Log.w(TAG, "OTA network failure for ${redactedHost(url)} (${t.javaClass.simpleName})")
        return BundleException("NETWORK", "network error (${t.javaClass.simpleName})")
    }

    private fun isPinFailure(t: Throwable?): Boolean {
        var cur = t
        var depth = 0
        while (cur != null && depth < 8) {
            if (cur is PinMismatchException || cur is SSLPeerUnverifiedException) return true
            cur = cur.cause
            depth++
        }
        return false
    }

    private fun safeProbeFp(url: String): String? = try {
        PinnedClient.probeLeafFingerprint(url)
    } catch (_: Exception) {
        null
    }

    // ---- helpers ----------------------------------------------------------------------

    private fun sameHostPort(a: String, b: String): Boolean = try {
        val ua = URI(a); val ub = URI(b)
        val pa = if (ua.port == -1) 443 else ua.port
        val pb = if (ub.port == -1) 443 else ub.port
        ua.host != null && ua.host.equals(ub.host, ignoreCase = true) && pa == pb
    } catch (_: Exception) {
        false
    }

    private fun redactedHost(url: String): String = try {
        val u = URI(url)
        "${u.host}:${if (u.port == -1) 443 else u.port}"
    } catch (_: Exception) {
        "<unparseable-url>"
    }

    private fun hexToBytes(hex: String): ByteArray? {
        if (hex.length != 64 || hex.any { it !in '0'..'9' && it !in 'a'..'f' }) return null
        return ByteArray(32) { i ->
            ((Character.digit(hex[2 * i], 16) shl 4) + Character.digit(hex[2 * i + 1], 16)).toByte()
        }
    }

    private fun deleteRecursively(f: File) {
        if (!f.exists()) return
        f.walkBottomUp().forEach { it.delete() }
    }

    companion object {
        private const val TAG = "WavrUpdate"
        private const val WEB_DIR = "wavr-web"
        private const val MAX_DOWNLOAD = 64L * 1024 * 1024       // hard ceiling on the .tar.gz
        private const val DOWNLOAD_TIMEOUT_S = 180L
        private const val READ_TIMEOUT_S = 60L

        // Keystore keys — MUST match the shim / wavr-secure-storage contract.
        private const val K_URL = "wavr.centralUrl"
        private const val K_FP = "wavr.pinnedFp"
        private const val K_TOKEN = "wavr.token"
    }
}
