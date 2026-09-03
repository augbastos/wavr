package dev.wavr.core

import android.content.Context
import android.util.Log
import java.io.File

/**
 * Puts the dashboard on a real filesystem path.
 *
 * The Core serves `frontend/index.html` with `FileResponse`, which needs a path
 * a POSIX `open()` can reach. Android assets are not that: they live compressed
 * inside the APK behind `AssetManager`, and Python cannot see them at all. So the
 * frontend is copied once into [Context.getFilesDir] and the Core is pointed at
 * the copy via `WAVR_FRONTEND`.
 *
 * Copied once **per app version**, tracked by a stamp file holding the
 * versionCode. An upgrade re-stages; a restart does not. That matters more than
 * it looks: `index.html` alone is ~1.1 MB and the vendor bundle another ~1 MB, so
 * re-copying on every Core start would add avoidable seconds and avoidable flash
 * wear to a device that is meant to run for months.
 *
 * PRIVACY: this only ever moves files *out of the APK* — read-only, signed,
 * shipped content. Nothing the Core produces is written here, and the whole
 * directory is app-private and removed by uninstall.
 */
object AssetStaging {

    private const val TAG = "WavrAssets"

    /** Directory inside the APK. Empty in the kiosk-only build. */
    private const val ASSET_ROOT = "frontend"

    /** Where it lands. Must match `WAVR_FRONTEND`. */
    private const val TARGET_DIR = "frontend"

    private const val STAMP = ".staged-version"

    /**
     * Ensure `filesDir/frontend` matches the APK's copy, and return it.
     *
     * Returns the target directory even on failure — the Core then serves the
     * API and 404s on `GET /`, which is a visible, diagnosable failure. Silently
     * returning a path that is not the frontend would be worse.
     */
    fun ensureFrontend(context: Context): File {
        val target = File(context.filesDir, TARGET_DIR)
        val stamp = File(target, STAMP)
        val version = versionCode(context).toString()

        try {
            if (stamp.isFile && stamp.readText().trim() == version &&
                File(target, "index.html").isFile
            ) {
                return target
            }
            if (target.exists()) target.deleteRecursively()
            target.mkdirs()
            val copied = copyAssetDir(context, ASSET_ROOT, target)
            if (copied == 0) {
                Log.w(TAG, "no frontend assets in this build; dashboard will 404")
            } else {
                stamp.writeText(version)
                Log.i(TAG, "staged $copied dashboard files")
            }
        } catch (t: Throwable) {
            Log.w(TAG, "staging failed: ${t.javaClass.simpleName}")
        }
        return target
    }

    /** @return number of files written. */
    private fun copyAssetDir(context: Context, assetPath: String, dest: File): Int {
        val am = context.assets
        val children = try {
            am.list(assetPath) ?: emptyArray()
        } catch (t: Throwable) {
            return 0
        }
        // `list()` returns an empty array for a FILE as well as for an empty
        // directory, so a leaf is detected by successfully opening it.
        if (children.isEmpty()) {
            return try {
                am.open(assetPath).use { input ->
                    dest.parentFile?.mkdirs()
                    dest.outputStream().use { input.copyTo(it) }
                }
                1
            } catch (t: Throwable) {
                0
            }
        }
        var count = 0
        dest.mkdirs()
        for (child in children) {
            count += copyAssetDir(context, "$assetPath/$child", File(dest, child))
        }
        return count
    }

    private fun versionCode(context: Context): Long = try {
        val info = context.packageManager.getPackageInfo(context.packageName, 0)
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }
    } catch (t: Throwable) {
        0L
    }
}
