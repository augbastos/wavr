package dev.wavr.mobile.wavrupdate

import java.io.File
import java.io.IOException
import java.io.InputStream

/**
 * A typed failure from bundle install — carries a machine code the plugin maps to a
 * JS rejection code (see wavr-update.d.ts). Kept in this file so both SafeTarExtractor
 * and BundleInstaller can throw it without a cyclic import.
 */
class BundleException(
    val code: String,
    message: String,
    /** Best-effort fingerprint the server is NOW presenting; set only for PIN_MISMATCH,
     *  so the plugin can hand the shim old-vs-new (same shape as WavrNet). */
    val presentedFp: String? = null,
) : IOException(message)

/**
 * SafeTarExtractor — a deliberately MINIMAL, HARDENED ustar (POSIX tar) reader.
 *
 * Why hand-rolled instead of a tar library: it adds ZERO new native dependency (no
 * .so, nothing for the 16 KB-page-size audit, no license surface) and — more
 * importantly — gives line-by-line control over EVERY safe-extraction check. The
 * archive being unpacked came over the network; treat every field as hostile.
 *
 * FAIL-CLOSED RULES (any violation throws BundleException("UNSAFE_BUNDLE"), and the
 * caller deletes the half-written staging dir):
 *  - Only plain files (typeflag '0'/'\0') and directories ('5') are accepted. Symlink
 *    ('2'), hardlink ('1'), char/block/fifo ('3'/'4'/'6'), and GNU long-name/extended
 *    ('L'/'K'/'x'/'g') entries are REFUSED — no link can smuggle a path out of the dir.
 *  - Paths are validated BEFORE any byte is written: no absolute paths, no '..'
 *    component, no backslashes, no NUL, no drive letters; and the resolved canonical
 *    target must stay inside the destination directory (defense in depth).
 *  - Every FILE must have an allowed web-asset extension; a bundle carrying any other
 *    entry is rejected wholesale. The shim/lib basenames are refused even though '.js'
 *    is allowed — the code that establishes the pin never rides the channel it secures.
 *  - Entry count, per-entry size, and total uncompressed size are all capped
 *    (zip-bomb guard on the decompressed side; the compressed side is capped in
 *    BundleInstaller).
 *  - A root "index.html" must be present or the bundle is rejected (Capacitor needs it).
 *
 * The reader consumes a plain InputStream (the caller wraps the verified .tar.gz in a
 * GZIPInputStream), so this class never sees the network or the gzip layer.
 */
object SafeTarExtractor {

    private const val BLOCK = 512
    private const val MAX_ENTRIES = 4000
    private const val MAX_ENTRY_BYTES = 32L * 1024 * 1024      // 32 MiB per file
    private const val MAX_TOTAL_BYTES = 64L * 1024 * 1024      // 64 MiB unpacked total

    /** Lowercase extensions a web bundle may contain. Anything else => reject. */
    private val ALLOWED_EXT = setOf(
        "html", "htm", "js", "mjs", "css", "json", "webmanifest", "map",
        "svg", "png", "jpg", "jpeg", "gif", "webp", "ico", "bmp",
        "woff", "woff2", "ttf", "otf", "eot", "txt", "wasm", "xml"
    )

    /** Basenames that must NEVER arrive over OTA even though '.js' is allowed:
     *  the shim/lib hold the pin and native trust wiring — they ship via the APK. */
    private val FORBIDDEN_BASENAMES = setOf("wavr-mobile-shim.js", "wavr-lib.js")

    /**
     * Extract [tar] into [destDir] (created fresh by the caller). Throws
     * BundleException("UNSAFE_BUNDLE") on any violation. On success the caller has a
     * fully-populated directory containing a root index.html.
     */
    fun extract(tar: InputStream, destDir: File) {
        val destCanonical = destDir.canonicalFile
        val header = ByteArray(BLOCK)
        var entries = 0
        var totalBytes = 0L
        var sawIndex = false

        while (true) {
            if (!readFully(tar, header)) break            // clean EOF between records
            if (isZeroBlock(header)) break                // end-of-archive marker

            entries++
            if (entries > MAX_ENTRIES) {
                throw BundleException("UNSAFE_BUNDLE", "too many entries")
            }

            val name = combinedName(header)
            val size = parseOctalSize(header)
            val type = header[156].toInt().toChar()

            when (type) {
                '5' -> {                                  // directory
                    validatePathComponents(name)
                    val dir = safeResolve(destCanonical, name)
                    if (!dir.isDirectory && !dir.mkdirs()) {
                        throw BundleException("STORAGE", "could not create a directory")
                    }
                    skipEntryData(tar, size)              // dirs carry no data, but be exact
                }
                '0', '\u0000' -> {                      // regular file ('0' or legacy NUL flag)
                    validatePathComponents(name)
                    validateFileName(name)
                    if (size > MAX_ENTRY_BYTES) {
                        throw BundleException("UNSAFE_BUNDLE", "an entry exceeds the per-file cap")
                    }
                    totalBytes += size
                    if (totalBytes > MAX_TOTAL_BYTES) {
                        throw BundleException("UNSAFE_BUNDLE", "the bundle exceeds the total-size cap")
                    }
                    val out = safeResolve(destCanonical, name)
                    out.parentFile?.let { if (!it.isDirectory && !it.mkdirs()) throw BundleException("STORAGE", "could not create a directory") }
                    writeEntry(tar, out, size)
                    if (name == "index.html") sawIndex = true
                }
                else -> {
                    // symlink/hardlink/device/GNU-extended/anything else: fail closed.
                    throw BundleException("UNSAFE_BUNDLE", "the bundle contains a non-file entry")
                }
            }
        }

        if (!sawIndex) {
            throw BundleException("UNSAFE_BUNDLE", "the bundle has no root index.html")
        }
    }

    // ---- header parsing ---------------------------------------------------------------

    /** ustar splits long paths into prefix(345..500) + name(0..100). Join them. */
    private fun combinedName(h: ByteArray): String {
        val name = cString(h, 0, 100)
        val prefix = cString(h, 345, 155)
        val joined = if (prefix.isEmpty()) name else "$prefix/$name"
        return joined.trim().trimStart('/')          // leading '/' is re-checked below too
    }

    private fun parseOctalSize(h: ByteArray): Long {
        // size field: 12 bytes at offset 124, octal ASCII, space/NUL padded.
        // GNU base-256 (high bit set) is refused as suspicious — web assets are small.
        if (h[124].toInt() and 0x80 != 0) {
            throw BundleException("UNSAFE_BUNDLE", "unsupported size encoding")
        }
        // cString already stops at NUL; .trim() drops the ASCII-space padding.
        val raw = cString(h, 124, 12).trim()
        if (raw.isEmpty()) return 0L
        return try {
            raw.toLong(8)
        } catch (_: NumberFormatException) {
            throw BundleException("UNSAFE_BUNDLE", "malformed entry size")
        }
    }

    private fun cString(b: ByteArray, off: Int, len: Int): String {
        var end = off
        val limit = off + len
        while (end < limit && b[end].toInt() != 0) end++
        return String(b, off, end - off, Charsets.US_ASCII)
    }

    private fun isZeroBlock(b: ByteArray): Boolean = b.all { it.toInt() == 0 }

    // ---- path safety ------------------------------------------------------------------

    private fun validatePathComponents(name: String) {
        if (name.isEmpty()) throw BundleException("UNSAFE_BUNDLE", "empty entry name")
        if (name.contains('\u0000')) throw BundleException("UNSAFE_BUNDLE", "NUL in entry name")
        if (name.contains('\\')) throw BundleException("UNSAFE_BUNDLE", "backslash in entry name")
        if (name.startsWith('/')) throw BundleException("UNSAFE_BUNDLE", "absolute entry path")
        // Windows-style drive/UNC just in case a cross-tool produced them.
        if (name.length >= 2 && name[1] == ':') throw BundleException("UNSAFE_BUNDLE", "drive-letter path")
        for (part in name.split('/')) {
            if (part == "..") throw BundleException("UNSAFE_BUNDLE", "'..' in entry path")
        }
    }

    private fun validateFileName(name: String) {
        val base = name.substringAfterLast('/')
        if (base.lowercase() in FORBIDDEN_BASENAMES) {
            throw BundleException("UNSAFE_BUNDLE", "the bundle carries a forbidden file")
        }
        val ext = base.substringAfterLast('.', "").lowercase()
        if (ext.isEmpty() || ext !in ALLOWED_EXT) {
            throw BundleException("UNSAFE_BUNDLE", "the bundle carries a non-web asset")
        }
    }

    /** Resolve [name] under [dest] and confirm the canonical result stays inside. */
    private fun safeResolve(dest: File, name: String): File {
        val target = File(dest, name).canonicalFile
        val destPath = dest.path + File.separator
        if (target.path != dest.path && !target.path.startsWith(destPath)) {
            throw BundleException("UNSAFE_BUNDLE", "entry escapes the destination directory")
        }
        return target
    }

    // ---- data blocks ------------------------------------------------------------------

    private fun writeEntry(tar: InputStream, out: File, size: Long) {
        out.outputStream().use { fos ->
            copyExact(tar, fos, size)
        }
        skipPadding(tar, size)
    }

    private fun skipEntryData(tar: InputStream, size: Long) {
        if (size <= 0) return
        val sink = object : java.io.OutputStream() {
            override fun write(b: Int) {}
            override fun write(b: ByteArray, off: Int, len: Int) {}
        }
        copyExact(tar, sink, size)
        skipPadding(tar, size)
    }

    private fun copyExact(input: InputStream, out: java.io.OutputStream, size: Long) {
        val buf = ByteArray(64 * 1024)
        var remaining = size
        while (remaining > 0) {
            val want = minOf(remaining, buf.size.toLong()).toInt()
            val read = input.read(buf, 0, want)
            if (read < 0) throw BundleException("UNSAFE_BUNDLE", "truncated archive")
            out.write(buf, 0, read)
            remaining -= read
        }
    }

    /** Entry data is padded to the next 512-byte boundary; consume the pad. */
    private fun skipPadding(tar: InputStream, size: Long) {
        val rem = (size % BLOCK).toInt()
        if (rem == 0) return
        val pad = ByteArray(BLOCK - rem)
        if (!readFully(tar, pad)) throw BundleException("UNSAFE_BUNDLE", "truncated padding")
    }

    /** Read exactly buf.size bytes; false ONLY on immediate clean EOF (0 bytes read). */
    private fun readFully(input: InputStream, buf: ByteArray): Boolean {
        var off = 0
        while (off < buf.size) {
            val read = input.read(buf, off, buf.size - off)
            if (read < 0) return off != 0 && throwTruncated()
            off += read
        }
        return true
    }

    private fun throwTruncated(): Boolean =
        throw BundleException("UNSAFE_BUNDLE", "truncated archive record")
}
