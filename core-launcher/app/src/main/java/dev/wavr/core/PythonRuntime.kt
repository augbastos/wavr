package dev.wavr.core

import android.content.Context
import android.util.Log
import org.json.JSONObject

/**
 * The embedded Wavr Core runtime (ADR-0010): CPython, in this process, running
 * the unmodified `wavr` package out of `backend/`.
 *
 * WHY REFLECTION. Chaquopy is applied by a Gradle property (`-PwavrPython=true`)
 * so that the plain kiosk keeps building for anyone who has not installed a host
 * CPython 3.13, and so today's 2 MB APK does not silently become 34 MB. That
 * means `com.chaquo.python.Python` is on the classpath in one build and absent in
 * the other, and the Kotlin here must compile identically either way. Reflection
 * across a four-method surface is the cheapest honest way to express "this
 * runtime may or may not be bundled" — and [Absent] is not a stub that pretends,
 * it is a runtime that says out loud that it is not there.
 *
 * WHAT THIS DOES NOT DO. It does not interpret, translate, or wrap the Core's
 * behaviour. `start()` hands over an environment map and gets back JSON; every
 * decision behind that call is Python's. If this file ever grows logic about
 * fusion, devices, or pairing, ADR-0010 has been violated.
 */
interface PythonRuntime {

    /** "chaquopy" or "absent" — surfaced to the panel, never inferred. */
    val kind: String

    /** True when an interpreter is actually bundled in this APK. */
    fun bundled(): Boolean

    /**
     * Boot the interpreter (once per process) and start the Core's uvicorn
     * server with [env] applied to `os.environ`.
     *
     * @return the JSON `wavr_android.start()` returned, or a JSON error object.
     *   Never throws — a Core that failed to start must report why, not crash
     *   the service that supervises it.
     */
    fun start(context: Context, env: Map<String, String>): String

    /**
     * Ask the Core to stop serving. This shuts down uvicorn; it does NOT tear
     * down the interpreter, because CPython embedded via Chaquopy cannot be
     * restarted within a process. Saying so plainly matters: "stopped" here
     * means the socket is closed and the fusion loop is idle, not that the
     * 21 MB of Python has left memory. Only killing the process does that, and
     * [CoreService.stopSelf] is what actually does it.
     */
    fun stop(): String

    /** `{"kind":..,"bundled":..,"running":..,"serving":..,"port":..,"error":..}` */
    fun status(): String

    companion object {
        /** The only place that decides which implementation you get. */
        fun resolve(): PythonRuntime =
            if (Chaquopy.isOnClasspath()) Chaquopy else Absent
    }

    /** Honest nothing. Present in every build; used in the kiosk-only build. */
    object Absent : PythonRuntime {
        override val kind = "absent"
        override fun bundled() = false
        override fun start(context: Context, env: Map<String, String>) =
            runtimeError(
                "no_runtime",
                "This build of Wavr Core does not bundle a Python runtime. " +
                    "Rebuild with -PwavrPython=true, or point this device at a Core on your network."
            )

        override fun stop() = runtimeError("no_runtime", "Nothing to stop.")
        override fun status(): String = JSONObject().apply {
            put("kind", kind)
            put("bundled", false)
            put("running", false)
            put("serving", false)
        }.toString()
    }

    /**
     * Chaquopy, reached reflectively. Mirrors exactly this Java surface:
     *
     * ```java
     * if (!Python.isStarted()) Python.start(new AndroidPlatform(context));
     * Python.getInstance().getModule("wavr_android").callAttr("start", envJson);
     * ```
     */
    object Chaquopy : PythonRuntime {

        private const val TAG = "WavrPy"
        private const val CLS_PYTHON = "com.chaquo.python.Python"
        private const val CLS_PLATFORM = "com.chaquo.python.Platform"
        private const val CLS_ANDROID_PLATFORM = "com.chaquo.python.android.AndroidPlatform"
        private const val BOOTSTRAP_MODULE = "wavr_android"

        override val kind = "chaquopy"

        @Volatile private var started = false

        fun isOnClasspath(): Boolean = try {
            Class.forName(CLS_PYTHON)
            true
        } catch (t: Throwable) {
            false
        }

        override fun bundled(): Boolean = isOnClasspath()

        @Synchronized
        override fun start(context: Context, env: Map<String, String>): String {
            return try {
                ensureInterpreter(context.applicationContext)
                val envJson = JSONObject().apply {
                    for ((k, v) in env) put(k, v)
                }.toString()
                callBootstrap("start", envJson)
            } catch (t: Throwable) {
                // The class name only. An exception message from deep inside the
                // Core could carry a path or a config value; the logcat rule for
                // this app is names, never payloads.
                Log.w(TAG, "Core start failed: ${t.javaClass.name}")
                runtimeError("start_failed", t.javaClass.simpleName)
            }
        }

        @Synchronized
        override fun stop(): String = try {
            if (!started) runtimeError("not_started", "The runtime was never started.")
            else callBootstrap("stop")
        } catch (t: Throwable) {
            Log.w(TAG, "Core stop failed: ${t.javaClass.name}")
            runtimeError("stop_failed", t.javaClass.simpleName)
        }

        override fun status(): String = try {
            if (!started) {
                JSONObject().apply {
                    put("kind", kind)
                    put("bundled", true)
                    put("running", false)
                    put("serving", false)
                }.toString()
            } else {
                callBootstrap("status")
            }
        } catch (t: Throwable) {
            runtimeError("status_failed", t.javaClass.simpleName)
        }

        /**
         * `capabilities.recommend()` applied to an Android-built manifest.
         *
         * The interpreter must already be running — this is called after the
         * Core is up, never as a way to boot it. Kotlin passes JSON in and gets
         * JSON out; the reasoning is entirely Python's.
         */
        fun recommend(manifestJson: String): String = try {
            if (!started) runtimeError("not_started", "The Core is not running yet.")
            else callBootstrap("recommend", manifestJson)
        } catch (t: Throwable) {
            runtimeError("recommend_failed", t.javaClass.simpleName)
        }

        // -----------------------------------------------------------------

        private fun ensureInterpreter(context: Context) {
            if (started) return
            val pythonCls = Class.forName(CLS_PYTHON)
            val isStarted = pythonCls.getMethod("isStarted").invoke(null) as Boolean
            if (!isStarted) {
                val platformCls = Class.forName(CLS_ANDROID_PLATFORM)
                val platform = platformCls
                    .getConstructor(Context::class.java)
                    .newInstance(context)
                pythonCls
                    .getMethod("start", Class.forName(CLS_PLATFORM))
                    .invoke(null, platform)
            }
            started = true
        }

        /** `Python.getInstance().getModule("wavr_android").callAttr(fn, *args)`. */
        private fun callBootstrap(fn: String, vararg args: String): String {
            val pythonCls = Class.forName(CLS_PYTHON)
            val instance = pythonCls.getMethod("getInstance").invoke(null)
            val module = pythonCls
                .getMethod("getModule", String::class.java)
                .invoke(instance, BOOTSTRAP_MODULE)
                ?: return runtimeError("no_module", BOOTSTRAP_MODULE)

            // callAttr(String, Object...) — the varargs array must be passed as
            // ONE argument or reflection spreads it and the arity is wrong.
            @Suppress("UNCHECKED_CAST")
            val result = module.javaClass
                .getMethod("callAttr", String::class.java, Array<Any>::class.java)
                .invoke(module, fn, args as Array<Any>)
            return result?.toString() ?: "{}"
        }
    }
}

/** Uniform JSON failure shape so the panel never has to parse two of them. */
private fun runtimeError(code: String, detail: String): String = JSONObject().apply {
    put("ok", false)
    put("error", code)
    put("detail", detail)
}.toString()
