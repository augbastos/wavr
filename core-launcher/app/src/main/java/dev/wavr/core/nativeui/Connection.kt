package dev.wavr.core.nativeui

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Which Core this app talks to. `pin` is the certificate fingerprint captured
 * when the person joined (trust on first use); `token` is the credential the
 * Core issued once. A Core on this same device needs neither.
 */
data class Connection(val url: String, val pin: String?, val token: String?) {
    val onThisDevice: Boolean get() = url.startsWith("http://127.0.0.1") || url.startsWith("https://127.0.0.1")

    companion object {
        val LOOPBACK = Connection("http://127.0.0.1:8000", null, null)

        /** A Core on this device serves plain HTTP, or HTTPS once LAN access is on
         *  (then on loopback too). Whichever answers is the one to use; `answers`
         *  is a blocking check, so call this off the main thread. */
        fun loopback(answers: (String) -> Boolean): Connection =
            listOf("http://127.0.0.1:8000", "https://127.0.0.1:8000")
                .firstOrNull(answers)?.let { Connection(it, null, null) } ?: LOOPBACK
    }
}

/**
 * The connection, kept in app-private preferences (the app has allowBackup
 * off), with the token encrypted by a key that lives in the Android Keystore
 * and never leaves it. A token that cannot be decrypted -- a restored backup,
 * a wiped keystore -- reads as "not joined", never as a half-working state.
 */
class ConnectionStore(context: Context) {
    private val prefs = context.getSharedPreferences("wavr_native_connection", Context.MODE_PRIVATE)

    /** The joined Core, or null when this device has not joined one. */
    fun joined(): Connection? {
        val url = prefs.getString("url", null) ?: return null
        val token = prefs.getString("token_ct", null)?.let { ct ->
            prefs.getString("token_iv", null)?.let { iv -> decrypt(ct, iv) }
        }
        if (token == null) return null
        return Connection(url, prefs.getString("pin", null), token)
    }

    /** The Core this app talks to: the joined one, else the one on this device. Blocking. */
    fun load(): Connection = joined() ?: Connection.loopback { url ->
        Snapshot.parse(WavrNative.snapshotFetch(url, null, null, 3000)).reachable == true
    }

    fun save(c: Connection) {
        val (ct, iv) = encrypt(c.token ?: "")
        prefs.edit().putString("url", c.url).putString("pin", c.pin)
            .putString("token_ct", ct).putString("token_iv", iv).apply()
    }

    fun forget() {
        prefs.edit().clear().apply()
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(ALIAS,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .build())
        return gen.generateKey()
    }

    private fun encrypt(plain: String): Pair<String, String> {
        val cipher = Cipher.getInstance(TRANSFORM).apply { init(Cipher.ENCRYPT_MODE, key()) }
        val ct = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(ct, Base64.NO_WRAP) to Base64.encodeToString(cipher.iv, Base64.NO_WRAP)
    }

    private fun decrypt(ct: String, iv: String): String? = try {
        val cipher = Cipher.getInstance(TRANSFORM).apply {
            init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)))
        }
        String(cipher.doFinal(Base64.decode(ct, Base64.NO_WRAP)), Charsets.UTF_8)
    } catch (_: Exception) {
        null
    }

    private companion object {
        const val ALIAS = "wavr_native_token"
        const val TRANSFORM = "AES/GCM/NoPadding"
    }
}
