package dev.wavr.sdk

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import dev.wavr.core.CorePrefs
import java.util.concurrent.Executors

/**
 * Reference experience #5: an Android application reading Wavr.
 *
 * Start it on a debuggable build with:
 *
 *     adb shell am start -n dev.wavr.core/dev.wavr.sdk.SpatialDemoActivity
 *
 * Deliberately not in the launcher and not exported. The product on this phone
 * is the Core and its kiosk panel; a second icon would be a developer tool
 * sitting in a household's app drawer, which is the platform complexity leaking
 * into the ordinary setup that Developer Mode exists to prevent.
 *
 * ## What it demonstrates, and the one line worth copying
 *
 * Discovery (loopback to the Core in this very process), connection, version
 * negotiation, the room context, and an experience session that reports when its
 * room or its targets change.
 *
 * The line worth copying is in [renderRoom]:
 *
 *     val text = room.occupancy?.let { "$it here" } ?: "someone is here"
 *
 * `occupancy` is nullable and the compiler makes you handle it. `null` means
 * nothing in that room can count — not that the room is empty — and an
 * application that writes `?: 0` there will one day tell somebody their house is
 * empty while three people are in it.
 *
 * ## Threading
 *
 * The SDK is blocking by design. Everything network-facing runs on a single
 * executor and only text touches the main thread, which is the shape any real
 * application needs anyway.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
class SpatialDemoActivity : Activity() {

    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private lateinit var body: LinearLayout
    private var sessionId: String? = null
    private var refresh: Runnable? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        body = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(28), dp(20), dp(28))
            setBackgroundColor(BG)
        }
        setContentView(ScrollView(this).apply {
            setBackgroundColor(BG)
            addView(body, ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT))
        })
        title = "Wavr — Spatial Demo"
        load()
    }

    override fun onResume() {
        super.onResume()
        // Poll rather than hold a socket: an Android app is asleep most of the
        // time, and a WebSocket kept open through Doze costs battery for events
        // nobody is on screen to see.
        refresh = Runnable {
            load()
            refresh?.let { main.postDelayed(it, POLL_MS) }
        }.also { main.postDelayed(it, POLL_MS) }
    }

    override fun onPause() {
        super.onPause()
        refresh?.let { main.removeCallbacks(it) }
    }

    override fun onDestroy() {
        super.onDestroy()
        val id = sessionId
        if (id != null) {
            io.execute { runCatching { client().closeSession(id) } }
        }
        io.shutdown()
    }

    private fun load() {
        io.execute {
            val lines = try {
                read()
            } catch (e: WavrException.Unreachable) {
                // The ordinary case on a phone: the Core is starting, or was
                // stopped from its notification. Not an error state to shout
                // about.
                listOf(Line("Core not reachable", HEAD),
                       Line("It may be starting, or stopped from the " +
                            "notification.", DIM))
            } catch (e: WavrException.Auth) {
                listOf(Line("Not authorised", HEAD),
                       Line("This Core is running with a local token. A " +
                            "settings screen, not a retry.", DIM))
            } catch (e: Throwable) {
                listOf(Line("Could not read the Space", HEAD),
                       Line(e.message ?: e.javaClass.simpleName, DIM))
            }
            main.post { render(lines) }
        }
    }

    /** Everything this screen shows, gathered off the main thread. */
    private fun read(): List<Line> {
        val wavr = client().connect()
        val out = mutableListOf<Line>()

        out.add(Line(wavr.space?.name ?: "This Space", TITLE))
        out.add(Line("protocol v${wavr.protocolVersion}" +
            if (wavr.protocolAhead) " — this Core is NEWER than the SDK" else "", DIM))

        val rooms = wavr.contexts()
        if (rooms.isEmpty()) {
            out.add(Line("No rooms have a reading yet.", DIM))
            return out
        }
        rooms.forEach { renderRoom(it, out) }

        // An experience session: Wavr reports when its room or its targets
        // change. It does not move this application's state anywhere.
        val occupied = rooms.firstOrNull { it.occupied == true } ?: rooms.first()
        val id = sessionId
            ?: wavr.openSession("android-spatial-demo", occupied.room)
                .also { sessionId = it }
        val events = wavr.observeSession(id, occupied.room)
        out.add(Line("Session", HEAD))
        out.add(Line(
            if (events.isEmpty()) "nothing changed since the last check"
            else events.joinToString("\n") { "${it.event} ${it.room}".trim() },
            DIM))
        return out
    }

    private fun renderRoom(room: RoomContext, out: MutableList<Line>) {
        out.add(Line(room.room, HEAD))

        // The line worth copying. `occupancy` is nullable and the compiler makes
        // you handle it: null means nothing here can COUNT, not that the room is
        // empty, and `?: 0` would one day report an empty house with three
        // people in it.
        val text = when {
            room.occupied == null -> "not observed"
            room.occupied == false -> "nobody detected"
            else -> room.occupancy?.let { n ->
                if (n == 1) "1 person here" else "$n people here"
            } ?: "someone is here — no headcount available"
        }
        out.add(Line(text, if (room.occupied == true) OK else BODY))
        out.add(Line("can: " + (room.capabilities.joinToString(", ")
            .ifEmpty { "nothing — no working sensor covers this room" }), DIM))
        room.limitations.forEach { out.add(Line("!  $it", WARN)) }
    }

    // -- The SDK calls, kept together so they read as one contract ------------

    private fun client() = WavrClient("http://127.0.0.1:${CorePrefs(this).port}")

    // -- Rendering ------------------------------------------------------------

    private data class Line(val text: String, val style: Int)

    private fun render(lines: List<Line>) {
        body.removeAllViews()
        lines.forEach { body.addView(textView(it)) }
    }

    private fun textView(line: Line): View = TextView(this).apply {
        text = line.text
        setTextColor(when (line.style) {
            TITLE, HEAD -> FG
            OK -> GREEN
            WARN -> AMBER
            else -> DIM_COLOR
        })
        setTextSize(TypedValue.COMPLEX_UNIT_SP, when (line.style) {
            TITLE -> 26f
            HEAD -> 18f
            else -> 14f
        })
        if (line.style == TITLE || line.style == HEAD) {
            setTypeface(typeface, Typeface.BOLD)
        }
        gravity = Gravity.START
        setPadding(0, if (line.style == HEAD) dp(20) else dp(3), 0, 0)
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private companion object {
        const val POLL_MS = 4000L
        const val TITLE = 0
        const val HEAD = 1
        const val BODY = 2
        const val DIM = 3
        const val OK = 4
        const val WARN = 5
        val BG = Color.parseColor("#0B0F14")
        val FG = Color.parseColor("#E6E8EC")
        val DIM_COLOR = Color.parseColor("#8A97A5")
        val GREEN = Color.parseColor("#4ADE80")
        val AMBER = Color.parseColor("#FBBF24")
    }
}
