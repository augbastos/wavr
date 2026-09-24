package dev.wavr.core.nativeui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModelProvider

class NativeStatusActivity : ComponentActivity() {
    private val model by lazy { ViewModelProvider(this)[StatusViewModel::class.java] }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { PhoneStatus(model) }
    }

    override fun onStart() { super.onStart(); model.start() }
    override fun onStop() { model.stop(); super.onStop() }
}

internal fun known(value: Any?): String = when (value) {
    null -> "unknown"
    is String -> value.ifBlank { "not set" }   // e.g. a Space with no name yet: not a blank line
    is Boolean -> if (value) "yes" else "no"
    else -> value.toString()
}

internal fun percent(value: Double?): String = value?.let { "${(it * 100).toInt()}%" } ?: "unknown"

@Composable
private fun PhoneStatus(model: StatusViewModel) {
    val state by model.state.collectAsState()
    var page by remember { mutableIntStateOf(0) }
    val pages = listOf("Status", "Rooms", "Attention", "Device")
    MaterialTheme {
        Scaffold(
            containerColor = WavrTokens.background,
            bottomBar = {
                NavigationBar(containerColor = WavrTokens.surface) {
                    pages.forEachIndexed { index, label ->
                        NavigationBarItem(
                            selected = page == index, onClick = { page = index },
                            icon = { Text("●", color = if (page == index) WavrTokens.healthy else WavrTokens.dim) },
                            label = { Text(label, color = WavrTokens.text) }
                        )
                    }
                }
            }
        ) { insets ->
            Column(
                Modifier.fillMaxSize().padding(insets).verticalScroll(rememberScrollState())
                    .padding(20.dp), verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                Text(pages[page], color = WavrTokens.text, fontSize = 28.sp, fontWeight = FontWeight.Bold)
                state.message?.let { Text(it, color = WavrTokens.unavailable, fontSize = 18.sp) }
                if (state.snapshot == null && state.message == null) {
                    Text("Waiting for native snapshot", color = WavrTokens.dim)
                }
                when (page) {
                    0 -> StatusPanel(state.snapshot)
                    1 -> RoomsPanel(state.snapshot)
                    2 -> AttentionPanel(state.snapshot)
                    3 -> DevicePanel(state.manifest)
                }
            }
        }
    }
}

@Composable
internal fun Fact(label: String, value: String, color: Color = WavrTokens.text, large: Boolean = false) {
    Column(Modifier.fillMaxWidth().background(WavrTokens.surface).padding(14.dp)) {
        Text(label, color = WavrTokens.dim, fontSize = if (large) 18.sp else 13.sp)
        Text(value, color = color, fontSize = if (large) 25.sp else 17.sp)
    }
}

@Composable
internal fun StatusPanel(snapshot: Snapshot?) {
    Fact("Space", known(snapshot?.runtime?.space))
    Fact("Runtime", known(snapshot?.runtime?.state), WavrTokens.forState(snapshot?.runtime?.state))
    Fact("Headline", known(snapshot?.runtime?.headline))
    snapshot?.error?.let { Fact("Error", it, WavrTokens.unavailable) }
    Text("Findings", color = WavrTokens.text, fontWeight = FontWeight.Bold)
    val findings = snapshot?.runtime?.findings
    if (findings == null) Fact("Findings", "unknown")
    else if (findings.isEmpty()) Fact("Findings", "none")
    else findings.forEach { finding ->
        Fact(known(finding.text), "${known(finding.state)} · ${known(finding.detail)}",
            WavrTokens.forState(finding.state))
    }
}

@Composable
internal fun RoomsPanel(snapshot: Snapshot?) {
    Fact("Rooms readable", known(snapshot?.roomsReadable))
    val rooms = snapshot?.rooms
    if (rooms == null) Fact("Rooms", "unknown")
    else if (rooms.isEmpty()) Fact("Rooms", "none reported")
    else rooms.forEach { room ->
        Text(known(room.room), color = WavrTokens.text, fontSize = 21.sp, fontWeight = FontWeight.Bold)
        Fact("Occupied", known(room.occupied))
        Fact("Confidence", percent(room.confidence))
        Fact("People", known(room.personCount))
        Fact("Precision", known(room.precisionLevel))
        Fact("Explanation", known(room.explanation))
        if (room.sources == null) Fact("Sources", "unknown")
        else if (room.sources.isEmpty()) Fact("Sources", "none reported")
        else room.sources.forEach { source ->
            Fact("Source: ${known(source.modality)}",
                "${known(source.health)} · age ${known(source.ageS)} s · presence ${known(source.presence)} · confidence ${percent(source.confidence)}")
        }
    }
}

@Composable
internal fun AttentionPanel(snapshot: Snapshot?) {
    val attention = snapshot?.attention
    Fact("Headline", known(attention?.headline))
    Fact("Total", known(attention?.total))
    val missed = attention?.couldNotCheck
    Fact("Could not check", missed?.joinToString { known(it) }?.ifEmpty { "none" } ?: "unknown")
    val items = attention?.items
    if (items == null) Fact("Items", "unknown")
    else if (items.isEmpty()) Fact("Items", "none")
    else items.groupBy { it.band }.forEach { (band, grouped) ->
        Text(known(band), color = WavrTokens.attention, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        grouped.forEach { Fact(known(it.title), "${known(it.detail)} · ${known(it.where)} · ${known(it.action)}") }
    }
}

@Composable
internal fun DevicePanel(manifest: DeviceManifest?) {
    Fact("Platform", known(manifest?.platform))
    Fact("Tier", known(manifest?.tier))
    Fact("Functions", manifest?.functions?.joinToString { known(it) }?.ifEmpty { "none" } ?: "unknown")
    val caps = manifest?.capabilities
    if (caps == null) Fact("Capabilities", "unknown")
    else if (caps.isEmpty()) Fact("Capabilities", "none reported")
    else caps.toSortedMap().forEach { (key, value) -> Fact(key, known(value)) }
}
