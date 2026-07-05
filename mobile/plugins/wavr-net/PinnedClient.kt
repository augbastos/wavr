package dev.wavr.mobile.wavrnet

import java.io.IOException
import java.net.InetSocketAddress
import java.net.URI
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSession
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager
import okhttp3.OkHttpClient

/**
 * Marker exception for a TLS pin failure, thrown by [PinnedClient]'s TrustManager.
 *
 * The plugin classifies errors by searching the cause chain for THIS type (not for
 * a generic SSLHandshakeException, which can also mean protocol-level failures),
 * so "PIN_MISMATCH" is reported if and only if the peer presented a certificate
 * whose SHA-256 fingerprint differs from the pinned one.
 */
class PinMismatchException(message: String) : CertificateException(message)

/**
 * Pinned OkHttp client factory for the Wavr central's self-signed LAN certificate.
 *
 * Trust model (deliberate, Play-policy compliant — this PINS verification, it does
 * not disable it):
 *  - EXACTLY ONE certificate is trusted: the leaf whose SHA-256(DER) equals the
 *    caller-supplied fingerprint captured at pairing time. No system trust store,
 *    no CA chain building, no fallback. Any other certificate => hard handshake
 *    failure (fail closed).
 *  - The hostname verifier defers to the same fingerprint match. Correct for a
 *    self-signed cert whose IP SAN goes stale when the central moves to a new
 *    DHCP address: the cert's IDENTITY (its hash) is the trust anchor, not its
 *    name claims. It never returns true unconditionally — it re-reads the peer
 *    leaf from the SSLSession and re-compares.
 *  - The fingerprint format matches the backend's `wavr.tls.cert_fingerprint()`
 *    byte-for-byte: SHA-256 over the DER certificate, uppercase hex. The backend
 *    renders it colon-separated ("AB:CD:.."); comparisons here are done on the
 *    NORMALIZED form (colons/whitespace stripped, uppercased) and, at handshake
 *    time, on raw digest bytes via a constant-time compare — so formatting can
 *    never cause a false mismatch.
 *
 * The pinned fingerprint is a PER-CALL parameter: this factory holds no state
 * about which cert to trust. request() and openSocket() are pinned by whatever
 * fingerprint the caller (the shim, reading its paired-central record) supplies.
 *
 * The ONLY non-throwing TLS path in this file is inside [probeLeafFingerprint],
 * which never speaks a byte of application protocol — see its comment.
 */
object PinnedClient {

    private const val CONNECT_TIMEOUT_S = 10L
    private const val READ_TIMEOUT_S = 30L     // WS pings (below) arrive well inside this
    private const val WRITE_TIMEOUT_S = 15L
    private const val PING_INTERVAL_S = 20L    // keeps sparse /ws/live streams alive + detects dead peers
    private const val PROBE_TIMEOUT_MS = 7_000

    // One client per pinned fingerprint (in practice: exactly one). Memoization only —
    // the trust decision still comes from the caller-supplied fingerprint every call.
    // Reusing the client lets OkHttp pool the TLS connection instead of re-handshaking
    // per request; every NEW handshake still goes through the pinned TrustManager.
    private val clientCache = ConcurrentHashMap<String, OkHttpClient>()

    // ---- fingerprint helpers (format contract with backend/wavr/tls.py) ----------

    /** Strip colons/whitespace and uppercase, e.g. "ab:cd" -> "ABCD". */
    fun normalizeFp(fp: String): String =
        fp.filterNot { it == ':' || it.isWhitespace() }.uppercase()

    /** True iff [normalized] is a plausible SHA-256 hex fingerprint (64 hex chars). */
    fun isValidFp(normalized: String): Boolean =
        normalized.length == 64 && normalized.all { it in '0'..'9' || it in 'A'..'F' }

    /**
     * SHA-256 fingerprint of [cert], formatted EXACTLY like the backend's
     * `cert_fingerprint()`: uppercase hex, colon-separated ("AB:CD:.."). Java's
     * `getEncoded()` returns the DER bytes — the same bytes the backend hashes —
     * so the two values are equal byte-for-byte for the same certificate.
     */
    fun fingerprintOf(cert: X509Certificate): String = toColonHex(sha256(cert.encoded))

    private fun sha256(bytes: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").digest(bytes)

    private fun toColonHex(digest: ByteArray): String =
        digest.joinToString(":") { "%02X".format(it) }

    private fun hexToBytes(hex: String): ByteArray =
        ByteArray(hex.length / 2) { i ->
            ((Character.digit(hex[2 * i], 16) shl 4) + Character.digit(hex[2 * i + 1], 16)).toByte()
        }

    // ---- the pinned trust path (the ONLY trust path for data traffic) ------------

    /**
     * Trusts EXACTLY the certificate whose SHA-256(DER) equals [pinnedDigest].
     * Anything else throws => TLS alert => handshake aborted BEFORE any
     * application data (tokens, tickets) leaves the device. Runs on EVERY new
     * TLS handshake (pooled connections were already verified at their handshake).
     */
    private class PinnedTrustManager(private val pinnedDigest: ByteArray) : X509TrustManager {

        override fun checkClientTrusted(chain: Array<X509Certificate>?, authType: String?) {
            // WavrNet is strictly a TLS client; the client-auth path must never be
            // reached. Fail closed if it somehow is.
            throw CertificateException("WavrNet does not accept TLS client certificates")
        }

        override fun checkServerTrusted(chain: Array<X509Certificate>?, authType: String?) {
            if (chain.isNullOrEmpty()) {
                throw CertificateException("server presented an empty certificate chain")
            }
            val presented = sha256(chain[0].encoded)
            if (!MessageDigest.isEqual(pinnedDigest, presented)) {  // constant-time compare
                throw PinMismatchException(
                    "server leaf certificate SHA-256 does not match the pinned fingerprint"
                )
            }
            // Match => trusted. Pin-identity model, deliberately:
            //  - no system trust store, no CA semantics, no chain building;
            //  - no validity-window check: the fingerprint IS the identity. When the
            //    central regenerates an expired cert, the fingerprint changes and this
            //    surfaces as PIN_MISMATCH => the deliberate human re-verify UX. A
            //    stale-but-identical cert is still the same key we paired with.
        }

        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }

    /**
     * Hostname check deferred to the pin: true IFF the session's peer leaf cert
     * matches [pinnedDigest]. Never unconditionally true; any failure to read the
     * peer cert => false (fail closed). Redundant with the TrustManager by design
     * (defense in depth — e.g. odd session-resumption paths).
     */
    private class PinnedHostnameVerifier(private val pinnedDigest: ByteArray) : HostnameVerifier {
        override fun verify(hostname: String?, session: SSLSession?): Boolean {
            val leaf = try {
                session?.peerCertificates?.firstOrNull() as? X509Certificate
            } catch (_: Exception) {   // SSLPeerUnverifiedException et al => fail closed
                null
            } ?: return false
            return MessageDigest.isEqual(pinnedDigest, sha256(leaf.encoded))
        }
    }

    /**
     * The pinned OkHttpClient for [pinnedFp] (colon-separated or bare hex; both
     * accepted, normalized before use). Serves BOTH https requests and wss
     * websockets: pingInterval only affects WS/h2 framing, and WS ping/pong
     * traffic keeps reads inside READ_TIMEOUT_S on sparse /ws/live streams.
     *
     * @throws IllegalArgumentException if [pinnedFp] is not 64 hex chars after
     *         normalization — callers map this to INVALID_ARGS, never to a
     *         permissive fallback client.
     */
    fun clientFor(pinnedFp: String): OkHttpClient {
        val normalized = normalizeFp(pinnedFp)
        require(isValidFp(normalized)) {
            "pinnedFp must be a SHA-256 certificate fingerprint (64 hex chars)"
        }
        return clientCache.computeIfAbsent(normalized) { fp -> buildPinnedClient(hexToBytes(fp)) }
    }

    private fun buildPinnedClient(pinnedDigest: ByteArray): OkHttpClient {
        val trustManager = PinnedTrustManager(pinnedDigest)
        val sslContext = SSLContext.getInstance("TLS")
        // ONLY the pinned TrustManager — no system trust store, no key managers.
        sslContext.init(null, arrayOf<X509TrustManager>(trustManager), SecureRandom())
        return OkHttpClient.Builder()
            .sslSocketFactory(sslContext.socketFactory, trustManager)
            .hostnameVerifier(PinnedHostnameVerifier(pinnedDigest))
            .connectTimeout(CONNECT_TIMEOUT_S, TimeUnit.SECONDS)
            .readTimeout(READ_TIMEOUT_S, TimeUnit.SECONDS)
            .writeTimeout(WRITE_TIMEOUT_S, TimeUnit.SECONDS)
            .pingInterval(PING_INTERVAL_S, TimeUnit.SECONDS)
            .build()
    }

    // ---- probe: an untrusted READ of the presented cert (never a data path) -------

    /**
     * PROBE-ONLY PATH — reads the SHA-256 fingerprint of the leaf certificate the
     * server at [url] presents, WITHOUT trusting it and WITHOUT carrying any data.
     *
     * Why this is safe (and why a reviewer cannot make it leak into the data path):
     *  - It is NOT an OkHttpClient. It is a raw [SSLSocket] handshake: no HTTP
     *    request line, no headers, no token — not one byte of application protocol
     *    is ever written. The socket is closed immediately after the handshake.
     *    There is no client object a caller could accidentally reuse for
     *    request()/openSocket(); the capture-only TrustManager below exists only
     *    as an anonymous object inside this function and is unreachable elsewhere.
     *  - Its output is used for OUT-OF-BAND verification only: the human compares
     *    the returned fingerprint against the one shown on the central's trusted
     *    loopback dashboard (backend `/api/pair-code` -> `cert_fingerprint`), and
     *    for the old-vs-new display on a PIN_MISMATCH. Calling probe() establishes
     *    no trust anywhere: nothing is stored, nothing is re-pinned.
     *
     * Accepts https:// and wss:// URLs (same TLS either way).
     *
     * @throws IllegalArgumentException for a non-https/wss or hostless URL.
     * @throws IOException if the server is unreachable or presents no certificate.
     */
    fun probeLeafFingerprint(url: String): String {
        val target = parseHostPort(url)
        val captured = arrayOfNulls<X509Certificate>(1)
        // Capture-only TrustManager: records the leaf, deliberately does NOT throw,
        // so the handshake completes far enough to read the cert. NEVER used to build
        // a client — see the function comment above.
        val captureOnlyTm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>?, authType: String?) {
                throw CertificateException("probe is TLS-client-only")
            }

            override fun checkServerTrusted(chain: Array<X509Certificate>?, authType: String?) {
                if (!chain.isNullOrEmpty()) captured[0] = chain[0]
                // no throw: this handshake exists only to READ the presented leaf
            }

            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        }
        val sslContext = SSLContext.getInstance("TLS")
        sslContext.init(null, arrayOf<X509TrustManager>(captureOnlyTm), SecureRandom())
        val socket = sslContext.socketFactory.createSocket() as SSLSocket
        try {
            socket.soTimeout = PROBE_TIMEOUT_MS
            socket.connect(InetSocketAddress(target.host, target.port), PROBE_TIMEOUT_MS)
            socket.startHandshake()
        } finally {
            try {
                socket.close()   // close immediately: the handshake was the whole point
            } catch (_: IOException) {
            }
        }
        val leaf = captured[0]
            ?: throw IOException("server presented no certificate during the handshake")
        return fingerprintOf(leaf)
    }

    private data class HostPort(val host: String, val port: Int)

    private fun parseHostPort(url: String): HostPort {
        val uri = try {
            URI(url)
        } catch (e: Exception) {
            throw IllegalArgumentException("malformed URL", e)
        }
        val scheme = uri.scheme?.lowercase()
        require(scheme == "https" || scheme == "wss") {
            "probe requires an https:// or wss:// URL (cleartext is refused)"
        }
        // URI keeps brackets around IPv6 literals; InetSocketAddress wants them bare.
        val host = uri.host?.removeSurrounding("[", "]")
            ?: throw IllegalArgumentException("URL has no host")
        val port = if (uri.port == -1) 443 else uri.port
        return HostPort(host, port)
    }
}
