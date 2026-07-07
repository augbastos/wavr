package dev.wavr.core

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import org.json.JSONObject
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * On-device camera -> local MJPEG source for the Wavr backend.
 *
 * The phone's OWN camera is turned into a `multipart/x-mixed-replace` stream
 * served over a raw [ServerSocket] bound to **127.0.0.1 only** (loopback) so the
 * frames can NEVER be reached off-device. The Wavr backend runs in a proot that
 * shares this app's network namespace, so it reads `http://localhost:8081/video`.
 *
 * PRIVACY INVARIANT (load-bearing — this is what separates Wavr from a
 * surveillance cam):
 *  - At app launch NOTHING is bound and the camera is CLOSED (green light off).
 *  - The camera device is opened ONLY between [start] and [stop]. [stop] releases
 *    the camera via `ProcessCameraProvider.unbindAll()` (green light off) and
 *    clears the last frame from memory. No frame is EVER written to disk.
 *  - The MJPEG server socket is bound lazily on the FIRST [start] and, once up,
 *    stays listening on loopback so `/video` can honestly answer `503` while the
 *    camera is off. With no camera bound there are simply no frames to serve, so
 *    a listening-but-idle loopback socket leaks nothing.
 *
 * Threading:
 *  - CameraX bind/unbind runs on the main thread (required).
 *  - Frame JPEG encoding runs on a single background analysis executor.
 *  - The HTTP accept loop + one thread per client run on background daemons.
 *  - Public methods here are safe to call from the WebView "JavaBridge" thread.
 *
 * Memory is capped to a single latest JPEG (no frame queue); encoding is
 * throttled to [TARGET_FPS] regardless of the sensor's native rate.
 */
class CameraMjpegStreamer(
    private val activity: FragmentActivity,
    /** Invoked (posted to main) to launch the runtime CAMERA permission dialog. */
    private val requestCameraPermission: () -> Unit,
) {

    enum class Lens(val jsName: String) {
        BACK("back"), FRONT("front");

        companion object {
            fun from(name: String?): Lens =
                if (name?.trim()?.lowercase() == "front") FRONT else BACK
        }
    }

    private companion object {
        const val TAG = "WavrCam"
        const val PORT = 8081
        const val PATH = "/video"
        const val TARGET_W = 640
        const val TARGET_H = 480
        const val TARGET_FPS = 8
        const val FRAME_INTERVAL_MS = 1000L / TARGET_FPS // ~125ms
        const val JPEG_QUALITY = 60
        const val BOUNDARY = "wavrframe"
        const val CLIENT_WAIT_MS = 2000L
    }

    private val mainHandler = Handler(Looper.getMainLooper())

    // ---- desired/actual state -------------------------------------------
    @Volatile private var desiredOn = false
    @Volatile private var streaming = false
    @Volatile private var lens: Lens = Lens.BACK
    @Volatile private var lastReason: String? = null

    // ---- latest-frame single buffer (no queue) --------------------------
    private val frameLock = Object()
    @Volatile private var latestJpeg: ByteArray? = null
    @Volatile private var frameSeq = 0L
    @Volatile private var lastEncodeAt = 0L

    // ---- CameraX --------------------------------------------------------
    private var cameraProvider: ProcessCameraProvider? = null
    private var analysis: ImageAnalysis? = null
    private var analysisExecutor: ExecutorService? = null

    // ---- loopback HTTP server -------------------------------------------
    @Volatile private var serverSocket: ServerSocket? = null
    @Volatile private var serverRunning = false
    private var serverThread: Thread? = null

    // =====================================================================
    // Public API (WebView bridge). Safe to call from the JavaBridge thread.
    // =====================================================================

    /** Turn the camera + stream on or off. Idempotent. */
    fun setEnabled(on: Boolean) {
        if (on) start() else stop()
    }

    /** Select the lens. If currently streaming, rebinds live. */
    fun setLens(name: String) {
        val next = Lens.from(name)
        if (next == lens) return
        lens = next
        if (streaming) mainHandler.post { bindCamera() }
    }

    /** `{"on":Boolean,"lens":"back|front","port":8081[,"reason":"..."]}` */
    fun state(): String = JSONObject().apply {
        put("on", streaming)
        put("lens", lens.jsName)
        put("port", PORT)
        lastReason?.let { put("reason", it) }
    }.toString()

    /** Full teardown for onDestroy: release camera AND close the server. */
    fun shutdown() {
        stop()
        stopServer()
    }

    /** Delivered from the Activity's permission launcher. */
    fun onPermissionResult(granted: Boolean) {
        if (!desiredOn) return
        if (granted) {
            lastReason = null
            doStart()
        } else {
            desiredOn = false
            lastReason = "permission_denied"
        }
    }

    // =====================================================================
    // Start / stop
    // =====================================================================

    private fun start() {
        desiredOn = true
        lastReason = null
        if (hasCameraPermission()) {
            doStart()
        } else {
            lastReason = "permission_pending"
            mainHandler.post { requestCameraPermission() }
        }
    }

    private fun doStart() {
        ensureServer()
        mainHandler.post { bindCamera() }
    }

    private fun stop() {
        desiredOn = false
        streaming = false
        // Wake any client writers so they observe streaming=false and finish.
        synchronized(frameLock) {
            latestJpeg = null   // drop residual frame from memory
            frameSeq++
            frameLock.notifyAll()
        }
        // Release the camera device on the main thread -> green light off.
        mainHandler.post {
            try {
                cameraProvider?.unbindAll()
            } catch (t: Throwable) {
                Log.w(TAG, "unbindAll failed: ${t.javaClass.simpleName}")
            }
        }
    }

    private fun hasCameraPermission(): Boolean =
        ContextCompat.checkSelfPermission(activity, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED

    // =====================================================================
    // CameraX binding (main thread)
    // =====================================================================

    private fun bindCamera() {
        if (!desiredOn) return
        val future = ProcessCameraProvider.getInstance(activity)
        future.addListener({
            if (!desiredOn) return@addListener
            try {
                val provider = future.get()
                cameraProvider = provider

                val exec = analysisExecutor ?: Executors.newSingleThreadExecutor()
                    .also { analysisExecutor = it }

                val resolution = ResolutionSelector.Builder()
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            Size(TARGET_W, TARGET_H),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER,
                        )
                    )
                    .build()

                val imageAnalysis = ImageAnalysis.Builder()
                    .setResolutionSelector(resolution)
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
                    .build()
                    .also { it.setAnalyzer(exec, ::onFrame) }
                analysis = imageAnalysis

                val selector = if (lens == Lens.FRONT) {
                    CameraSelector.DEFAULT_FRONT_CAMERA
                } else {
                    CameraSelector.DEFAULT_BACK_CAMERA
                }

                provider.unbindAll()
                provider.bindToLifecycle(activity, selector, imageAnalysis)
                streaming = true
                lastReason = null
            } catch (t: Throwable) {
                streaming = false
                lastReason = "camera_error"
                Log.w(TAG, "bindCamera failed: ${t.javaClass.simpleName}")
            }
        }, ContextCompat.getMainExecutor(activity))
    }

    // =====================================================================
    // Frame analysis -> JPEG (single-frame background executor)
    // =====================================================================

    private fun onFrame(image: ImageProxy) {
        try {
            if (!streaming) return
            val now = System.currentTimeMillis()
            if (now - lastEncodeAt < FRAME_INTERVAL_MS) return // throttle to TARGET_FPS
            lastEncodeAt = now

            val jpeg = encodeJpeg(image) ?: return
            synchronized(frameLock) {
                latestJpeg = jpeg
                frameSeq++
                frameLock.notifyAll()
            }
        } catch (t: Throwable) {
            Log.w(TAG, "onFrame failed: ${t.javaClass.simpleName}")
        } finally {
            image.close() // reuse the underlying buffer; never queue frames
        }
    }

    /** YUV_420_888 -> NV21 -> JPEG. Handles arbitrary row/pixel strides. */
    private fun encodeJpeg(image: ImageProxy): ByteArray? {
        if (image.format != ImageFormat.YUV_420_888) return null
        val nv21 = yuv420ToNv21(image)
        val yuv = YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null)
        val out = ByteArrayOutputStream(48 * 1024)
        yuv.compressToJpeg(Rect(0, 0, image.width, image.height), JPEG_QUALITY, out)
        return out.toByteArray()
    }

    private fun yuv420ToNv21(image: ImageProxy): ByteArray {
        val width = image.width
        val height = image.height
        val ySize = width * height
        val nv21 = ByteArray(ySize + ySize / 2)

        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        // --- Y plane (absolute indexing; robust to row padding) ---
        val yBuf = yPlane.buffer
        val yRowStride = yPlane.rowStride
        val yPixelStride = yPlane.pixelStride
        var pos = 0
        for (row in 0 until height) {
            var idx = row * yRowStride
            for (col in 0 until width) {
                nv21[pos++] = yBuf.get(idx)
                idx += yPixelStride
            }
        }

        // --- Chroma -> interleaved V,U (NV21) ---
        val uBuf = uPlane.buffer
        val vBuf = vPlane.buffer
        val uvRowStride = uPlane.rowStride
        val uvPixelStride = uPlane.pixelStride
        val chromaW = width / 2
        val chromaH = height / 2
        for (row in 0 until chromaH) {
            var uIdx = row * uvRowStride
            var vIdx = row * uvRowStride
            for (col in 0 until chromaW) {
                nv21[pos++] = vBuf.get(vIdx)
                nv21[pos++] = uBuf.get(uIdx)
                uIdx += uvPixelStride
                vIdx += uvPixelStride
            }
        }
        return nv21
    }

    // =====================================================================
    // Loopback MJPEG HTTP server
    // =====================================================================

    @Synchronized
    private fun ensureServer() {
        if (serverRunning) return
        try {
            val ss = ServerSocket()
            ss.reuseAddress = true
            // Bind to loopback ONLY. Frames must never be reachable off-device.
            ss.bind(InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT))
            serverSocket = ss
            serverRunning = true
            serverThread = Thread({ acceptLoop(ss) }, "wavr-mjpeg-accept")
                .apply { isDaemon = true; start() }
        } catch (t: Throwable) {
            serverRunning = false
            lastReason = "server_error"
            Log.w(TAG, "server bind failed: ${t.javaClass.simpleName}")
        }
    }

    private fun stopServer() {
        serverRunning = false
        try {
            serverSocket?.close()
        } catch (_: Throwable) {
        }
        serverSocket = null
    }

    private fun acceptLoop(ss: ServerSocket) {
        while (serverRunning) {
            val socket = try {
                ss.accept()
            } catch (t: Throwable) {
                if (!serverRunning || ss.isClosed) break else continue
            }
            Thread({ handleClient(socket) }, "wavr-mjpeg-client")
                .apply { isDaemon = true; start() }
        }
    }

    private fun handleClient(socket: Socket) {
        try {
            socket.tcpNoDelay = true
            val path = readRequestPath(socket.getInputStream())
            val out = BufferedOutputStream(socket.getOutputStream())

            if (path != PATH) {
                writeSimple(out, "404 Not Found", "not found")
                return
            }
            if (!streaming) {
                // Camera is off -> honestly report the source is unavailable.
                writeSimple(out, "503 Service Unavailable", "camera off")
                return
            }
            streamMjpeg(out, socket)
        } catch (_: Throwable) {
            // client gone / broken pipe — nothing to do
        } finally {
            try {
                socket.close()
            } catch (_: Throwable) {
            }
        }
    }

    /** Reads only the request line (first line) and returns its path token. */
    private fun readRequestPath(input: InputStream): String {
        val sb = StringBuilder(64)
        var c = input.read()
        while (c != -1 && c != '\n'.code) {
            if (c != '\r'.code) sb.append(c.toChar())
            if (sb.length > 1024) break // guard
            c = input.read()
        }
        // "GET /video HTTP/1.1" -> "/video"
        val parts = sb.toString().trim().split(" ")
        return if (parts.size >= 2) parts[1].substringBefore('?') else ""
    }

    private fun writeSimple(out: OutputStream, status: String, body: String) {
        val bytes = body.toByteArray(Charsets.UTF_8)
        val header = "HTTP/1.0 $status\r\n" +
            "Server: WavrCore\r\n" +
            "Content-Type: text/plain\r\n" +
            "Content-Length: ${bytes.size}\r\n" +
            "Connection: close\r\n" +
            "\r\n"
        out.write(header.toByteArray(Charsets.US_ASCII))
        out.write(bytes)
        out.flush()
    }

    private fun streamMjpeg(out: OutputStream, socket: Socket) {
        val header = "HTTP/1.0 200 OK\r\n" +
            "Server: WavrCore\r\n" +
            "Cache-Control: no-cache, private\r\n" +
            "Pragma: no-cache\r\n" +
            "Connection: close\r\n" +
            "Content-Type: multipart/x-mixed-replace; boundary=$BOUNDARY\r\n" +
            "\r\n"
        out.write(header.toByteArray(Charsets.US_ASCII))
        out.flush()

        var lastSeq = -1L
        while (serverRunning && streaming && !socket.isClosed) {
            val frame: ByteArray?
            val seq: Long
            synchronized(frameLock) {
                val start = System.currentTimeMillis()
                while (streaming && frameSeq == lastSeq) {
                    val wait = CLIENT_WAIT_MS - (System.currentTimeMillis() - start)
                    if (wait <= 0) break
                    frameLock.wait(wait)
                }
                frame = latestJpeg
                seq = frameSeq
            }
            if (!streaming) break
            if (frame == null || seq == lastSeq) continue // timeout w/o a new frame
            lastSeq = seq

            val part = "--$BOUNDARY\r\n" +
                "Content-Type: image/jpeg\r\n" +
                "Content-Length: ${frame.size}\r\n" +
                "\r\n"
            out.write(part.toByteArray(Charsets.US_ASCII))
            out.write(frame)
            out.write("\r\n".toByteArray(Charsets.US_ASCII))
            out.flush()
        }
    }
}
