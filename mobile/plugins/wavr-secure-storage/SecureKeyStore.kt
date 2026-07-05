package dev.wavr.mobile.securestorage

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * SecureKeyStore — the AES-256-GCM secret engine behind [WavrSecureStoragePlugin].
 *
 * WHAT IT HOLDS: exactly the three small paired-central secrets the shim keeps —
 * "wavr.centralUrl", "wavr.pinnedFp", "wavr.token" — each encrypted at rest with a
 * key that never leaves the AndroidKeyStore (hardware-backed / StrongBox where the
 * device offers it). No plaintext ever touches disk; @capacitor/preferences is
 * deliberately NOT used.
 *
 * TRUST / DEPENDENCY POSTURE (Play + appsec):
 *  - Hand-rolled AES/GCM/NoPadding over the platform KeyStore only. NO
 *    androidx.security.crypto, NO Tink, NO extra .so — keeps the dependency surface,
 *    the 16 KB-page-size audit, and the license story minimal.
 *  - The KeyStore key is symmetric AES-256, purposes ENCRYPT|DECRYPT, GCM, no
 *    padding, and deliberately has NO setUserAuthenticationRequired: the Phase 1
 *    companion viewer must read the token in the foreground with no biometric
 *    prompt. (Adding auth-required is a future opt-in, hence the versioned alias.)
 *
 * CRYPTO CHOICES (reviewed line-by-line):
 *  - IV FRESHNESS: the key is created with the AndroidKeyStore default
 *    randomizedEncryptionRequired=true, which REFUSES a caller-supplied IV and makes
 *    the provider generate a fresh random 12-byte (96-bit) nonce on every ENCRYPT
 *    init. We read it back via cipher.iv. This guarantees "never reuse an IV" and is
 *    strictly stronger than a hand-rolled SecureRandom because the keystore itself
 *    enforces it. 96-bit is the GCM-recommended nonce length.
 *  - TAG: 128-bit GCM auth tag (the maximum). Java's GCM cipher appends the tag to
 *    the ciphertext, so doFinal() output = ciphertext || 16-byte tag.
 *  - AT-REST FORMAT: Base64(NO_WRAP) of [ 12-byte IV | ciphertext+tag ], one entry
 *    per key in the dedicated SharedPreferences file below. NO_WRAP => no newlines.
 *  - ALIAS is versioned ("wavr_secure_v1"): a future crypto change bumps the suffix
 *    rather than silently reinterpreting old blobs.
 *
 * LOGGING (logcat is hostile): this file NEVER logs a value, a key name, plaintext,
 * ciphertext, or the KeyStore material. The single defensive log line carries an
 * exception CLASS NAME only.
 */
class SecureKeyStore(context: Context) {

    // applicationContext: the prefs file is process-scoped and must outlive any Activity.
    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE)

    /**
     * Encrypt [value] under the KeyStore key and DURABLY persist it under [key].
     *
     * Returns the result of SharedPreferences.Editor.commit() — the BLOCKING write,
     * NOT apply(). The caller (the plugin) resolves the JS promise only when this
     * returns true, so an awaited set() cannot resolve until the bytes are on disk.
     * That is load-bearing: index.html calls location.reload() right after a
     * successful pair. Throws only on a genuine crypto/KeyStore failure, which the
     * plugin maps to a rejection (never a silent success).
     */
    fun set(key: String, value: String): Boolean {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
        val iv = cipher.iv                                   // fresh 12-byte nonce (see header)
        val sealed = cipher.doFinal(value.toByteArray(Charsets.UTF_8))  // ciphertext || 128-bit tag
        val blob = ByteArray(iv.size + sealed.size)
        System.arraycopy(iv, 0, blob, 0, iv.size)
        System.arraycopy(sealed, 0, blob, iv.size, sealed.size)
        val encoded = Base64.encodeToString(blob, Base64.NO_WRAP)
        return prefs.edit().putString(key, encoded).commit()  // blocking commit(), not apply()
    }

    /**
     * Decrypt and return the plaintext for [key], or null when the entry is absent
     * OR (defensively) when it cannot be decrypted. NEVER throws on a miss — the JS
     * contract is get() resolves {value:null}, never rejects, on absence.
     */
    fun get(key: String): String? {
        val encoded = prefs.getString(key, null) ?: return null   // absent -> clean miss
        return try {
            val blob = Base64.decode(encoded, Base64.NO_WRAP)
            if (blob.size <= IV_LEN) return null                  // too short to hold IV+tag
            val iv = blob.copyOfRange(0, IV_LEN)
            val sealed = blob.copyOfRange(IV_LEN, blob.size)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
            String(cipher.doFinal(sealed), Charsets.UTF_8)
        } catch (e: Exception) {
            // Corrupt/tampered blob (AEADBadTagException), or the KeyStore key is gone
            // (app data cleared, cross-device restore where the hardware key can't
            // migrate). Treat as a miss so the shim re-pairs. Class name only — never
            // the key name, never the plaintext, never the ciphertext.
            Log.w(TAG, "decrypt miss (${e.javaClass.simpleName})")
            null
        }
    }

    /**
     * Delete [key]. Idempotent: an absent key is success. Uses commit() so a token
     * revoke (tokenSet(null) on a 401/403) is durably gone before we resolve.
     */
    fun remove(key: String): Boolean =
        if (!prefs.contains(key)) true
        else prefs.edit().remove(key).commit()

    /**
     * The AES-256 KeyStore key for [ALIAS], created on first use and reused forever
     * after. @Synchronized so a concurrent first-use cannot generate it twice (the
     * second caller finds and returns the existing key).
     */
    @Synchronized
    private fun getOrCreateKey(): SecretKey {
        val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        val existing = ks.getKey(ALIAS, null)
        if (existing is SecretKey) return existing               // reuse if present

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                // NO setUserAuthenticationRequired -> foreground reads need no biometric.
                // randomizedEncryptionRequired stays at its default (true) -> the keystore
                // forces a fresh random IV on every ENCRYPT and rejects caller-supplied IVs.
                .build()
        )
        return generator.generateKey()
    }

    companion object {
        private const val TAG = "WavrSecureStorage"
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val ALIAS = "wavr_secure_v1"      // versioned: bump on any crypto change
        private const val PREFS_FILE = "wavr_secure_store"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_TAG_BITS = 128            // 16-byte GCM auth tag (max)
        private const val IV_LEN = 12                   // 96-bit GCM nonce (keystore default)
    }
}
