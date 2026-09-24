package dev.wavr.core.nativeui

import android.os.Bundle
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.material3.Text
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModelProvider
import kotlinx.coroutines.delay

class TvStatusActivity : ComponentActivity() {
    private val model by lazy { ViewModelProvider(this)[StatusViewModel::class.java] }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { TvStatus(model) }
    }

    override fun onStart() { super.onStart(); model.start() }
    override fun onStop() { model.stop(); super.onStop() }
}

@Composable
private fun TvStatus(model: StatusViewModel) {
    val state by model.state.collectAsState()
    var panel by remember { mutableIntStateOf(0) }
    var ambient by remember { mutableStateOf(false) }
    var interaction by remember { mutableIntStateOf(0) }
    val firstTab = remember { FocusRequester() }
    val names = listOf("Status", "Rooms", "Attention")

    LaunchedEffect(interaction) {
        delay(60_000)
        ambient = true
    }
    LaunchedEffect(Unit) { firstTab.requestFocus() }

    Box(Modifier.fillMaxSize().background(WavrTokens.background)
        .onPreviewKeyEvent {
            if (it.nativeKeyEvent.action == KeyEvent.ACTION_DOWN) {
                ambient = false
                interaction++
            }
            false
        }) {
        Row(Modifier.fillMaxSize().alpha(if (ambient) 0f else 1f).padding(48.dp),
            horizontalArrangement = Arrangement.spacedBy(48.dp)) {
            // Proportional columns: a 10-foot layout must hold on any panel size,
            // not only 1920x1080 (it broke letter by letter on a narrow one).
            Column(Modifier.weight(0.3f), verticalArrangement = Arrangement.spacedBy(20.dp)) {
                Text("WAVR", color = WavrTokens.text, fontSize = 42.sp, fontWeight = FontWeight.Bold)
                names.forEachIndexed { index, label ->
                    var focused by remember { mutableStateOf(false) }
                    val tabModifier = Modifier
                        .then(if (index == 0) Modifier.focusRequester(firstTab) else Modifier)
                        .onFocusChanged { focused = it.isFocused }
                        .background(if (focused || panel == index) WavrTokens.elevated else WavrTokens.surface)
                        .clickable { panel = index; ambient = false; interaction++ }
                        .focusable()
                        .padding(horizontal = 28.dp, vertical = 20.dp)
                    Text(label, modifier = tabModifier,
                        color = if (focused) WavrTokens.healthy else WavrTokens.text,
                        fontSize = 28.sp)
                }
            }
            Column(Modifier.weight(0.7f).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(24.dp)) {
                Text(names[panel], color = WavrTokens.text, fontSize = 46.sp, fontWeight = FontWeight.Bold)
                state.message?.let { TvLine("Runtime", it, WavrTokens.unavailable) }
                when (panel) {
                    0 -> TvStatusPanel(state.snapshot)
                    1 -> TvRoomsPanel(state.snapshot)
                    else -> TvAttentionPanel(state.snapshot)
                }
            }
        }
        if (ambient) AmbientPanel(state)
    }
}

@Composable
private fun TvLine(label: String, value: String, color: Color = WavrTokens.text) {
    Column(Modifier.fillMaxWidth().background(WavrTokens.surface).padding(22.dp)) {
        Text(label, color = WavrTokens.dim, fontSize = 20.sp)
        Text(value, color = color, fontSize = 30.sp)
    }
}

@Composable
private fun TvStatusPanel(snapshot: Snapshot?) {
    TvLine("Space", known(snapshot?.runtime?.space))
    TvLine("State", known(snapshot?.runtime?.state), WavrTokens.forState(snapshot?.runtime?.state))
    TvLine("Headline", known(snapshot?.runtime?.headline))
    snapshot?.error?.let { TvLine("Error", it, WavrTokens.unavailable) }
    val findings = snapshot?.runtime?.findings
    if (findings == null) TvLine("Findings", "unknown")
    else if (findings.isEmpty()) TvLine("Findings", "none")
    else findings.forEach { TvLine(known(it.text), "${known(it.state)} · ${known(it.detail)}") }
}

@Composable
private fun TvRoomsPanel(snapshot: Snapshot?) {
    val rooms = snapshot?.rooms
    if (rooms == null) TvLine("Rooms", "unknown")
    else if (rooms.isEmpty()) TvLine("Rooms", "none reported")
    else rooms.forEach { room ->
        TvLine(known(room.room),
            "Occupied ${known(room.occupied)} · confidence ${percent(room.confidence)} · people ${known(room.personCount)}")
        TvLine("Precision", known(room.precisionLevel))
        TvLine("Explanation", known(room.explanation))
        if (room.sources == null) TvLine("Sources", "unknown")
        else room.sources.forEach {
            TvLine(known(it.modality), "${known(it.health)} · age ${known(it.ageS)} s")
        }
    }
}

@Composable
private fun TvAttentionPanel(snapshot: Snapshot?) {
    val attention = snapshot?.attention
    TvLine("Headline", known(attention?.headline))
    TvLine("Could not check",
        attention?.couldNotCheck?.joinToString { known(it) }?.ifEmpty { "none" } ?: "unknown")
    val items = attention?.items
    if (items == null) TvLine("Items", "unknown")
    else if (items.isEmpty()) TvLine("Items", "none")
    else items.groupBy { it.band }.forEach { (band, group) ->
        TvLine("Band", known(band), WavrTokens.attention)
        group.forEach { TvLine(known(it.title), known(it.detail)) }
    }
}

@Composable
private fun AmbientPanel(state: StatusUiState) {
    val snapshot = state.snapshot
    val occupied = snapshot?.rooms?.takeIf { snapshot?.roomsReadable == true &&
        it.all { room -> room.occupied != null } }?.count { it.occupied == true }
    Column(Modifier.fillMaxSize().padding(70.dp), verticalArrangement = Arrangement.Center) {
        Text("WAVR", color = WavrTokens.dim, fontSize = 26.sp)
        Text(known(snapshot?.runtime?.state),
            color = WavrTokens.forState(snapshot?.runtime?.state), fontSize = 70.sp)
        Text("Occupied rooms: ${known(occupied)}", color = WavrTokens.text, fontSize = 36.sp)
        state.message?.let { Text(it, color = WavrTokens.unavailable, fontSize = 26.sp) }
    }
}
