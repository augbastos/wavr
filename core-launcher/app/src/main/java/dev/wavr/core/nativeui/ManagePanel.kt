package dev.wavr.core.nativeui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Manage: which Core this device talks to (and joining one), privacy and
 * sensing switches, approvals when this device IS the Core, and diagnosis.
 * Every button runs one command; nothing here decides what is allowed.
 */
@Composable
internal fun ManagePanel(model: ManageViewModel) {
    val s by model.state.collectAsState()
    LaunchedEffect(s.connection) { model.refresh() }
    s.message?.let { Fact("Last action", it, if (s.busy) WavrTokens.dim else WavrTokens.attention) }

    Section("Connection")
    Fact("Core", s.connection.url)
    if (s.connection.pin != null) Fact("Certificate pin", groupFingerprint(s.connection.pin))
    else if (s.connection.onThisDevice) Fact("Trust", "this device (loopback)")
    // Whatever was joined -- even this device's own Core -- can be forgotten.
    if (s.connection.token != null) OutlinedButton(onClick = model::forget) { Text("Forget this Core") }
    if (s.connection.token == null) JoinSection(model, s)

    Section("Privacy and sensing")
    Toggle("Watch (flag people Wavr does not recognise)", s.watch) { model.setWatch(it) }
    Toggle("Sensing", s.sensing?.running) { model.setSensing(it) }
    val sources = s.sensing?.sources
    if (sources == null) Fact("Sources", "unknown")
    else sources.forEach { src ->
        Toggle("${src.name} · ${known(src.state)}", src.enabled) { model.setSource(src.name, it) }
    }

    if (s.connection.onThisDevice) ApprovalsSection(model, s)

    Section("Diagnosis")
    Button(onClick = { model.runDoctor() }, enabled = !s.busy) { Text("Run the Core's checks") }
    s.doctor?.forEach { c ->
        val word = when (c.ok) { true -> "ok"; false -> known(c.severity); null -> "not applicable" }
        Fact(known(c.id), "$word · ${known(c.detail)}",
            when (c.ok) { true -> WavrTokens.healthy; false -> WavrTokens.attention; null -> WavrTokens.dim })
    }
}

@Composable
private fun JoinSection(model: ManageViewModel, s: ManageState) {
    Section("Join a Space")
    val context = androidx.compose.ui.platform.LocalContext.current
    LaunchedEffect(Unit) { model.discover(context) }
    var url by remember { mutableStateOf("https://") }
    s.discovered.forEach { found ->
        OutlinedButton(onClick = { url = found; model.check(found) }) { Text("Found: $found") }
    }
    OutlinedTextField(url, { url = it }, label = { Text("Core address") }, singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth())
    val j = s.join
    if (j?.fingerprint == null) {
        Button(onClick = { model.check(url) }, enabled = !s.busy) { Text("Check its certificate") }
        return
    }
    Fact("Certificate the Core presented", groupFingerprint(j.fingerprint))
    Text("Compare it with the fingerprint on the Core's own screen before going on.",
        color = WavrTokens.dim, fontSize = 14.sp)
    if (j.compareCode == null) {
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(onClick = model::askToJoin) { Text("They match: ask to join") }
            OutlinedButton(onClick = model::cancelJoin) { Text("Cancel") }
        }
    } else {
        Text("Code for the person at the Core", color = WavrTokens.dim, fontSize = 14.sp)
        Text(j.compareCode.chunked(3).joinToString(" "), color = WavrTokens.text, fontSize = 44.sp,
            fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Bold,
            modifier = Modifier.semantics { contentDescription = "Compare code ${j.compareCode}" })
        Text("Waiting for them to type it on the Core...", color = WavrTokens.dim)
        OutlinedButton(onClick = model::cancelJoin) { Text("Cancel") }
    }
}

@Composable
private fun ApprovalsSection(model: ManageViewModel, s: ManageState) {
    Section("Devices asking to join")
    val pairings = s.pairings
    if (pairings == null) Fact("Requests", "unknown")
    else if (pairings.isEmpty()) Fact("Requests", "none")
    else pairings.forEach { p ->
        var code by remember(p.requestId) { mutableStateOf("") }
        var central by remember(p.requestId) { mutableStateOf(false) }
        Column(Modifier.fillMaxWidth().background(WavrTokens.surface).padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("${known(p.name)} · ${known(p.platform)}", color = WavrTokens.text, fontSize = 18.sp)
            OutlinedTextField(code, { code = it }, label = { Text("Code shown on that device") },
                singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
            Toggle("May change settings (central)", central) { central = it }
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = { model.approvePairing(p.requestId, code, if (central) "central" else "user") },
                    enabled = code.count(Char::isDigit) == 6) { Text("Let it in") }
                OutlinedButton(onClick = { model.denyPairing(p.requestId) }) { Text("Deny") }
            }
        }
    }
    Section("Sensors asking to join")
    val nodes = s.nodes
    if (nodes == null) Fact("Sensors", "unknown")
    else if (nodes.isEmpty()) Fact("Sensors", "none")
    else nodes.forEach { n ->
        // The sensor's own description is a claim: shown as a hint, never trusted.
        var name by remember(n.nodeId) { mutableStateOf("") }
        var type by remember(n.nodeId) { mutableStateOf("") }
        var room by remember(n.nodeId) { mutableStateOf("") }
        Column(Modifier.fillMaxWidth().background(WavrTokens.surface).padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("It says: ${known(n.nameHint)} · ${known(n.sensorHint)}", color = WavrTokens.dim)
            OutlinedTextField(name, { name = it }, label = { Text("Name") }, singleLine = true)
            OutlinedTextField(type, { type = it }, label = { Text("Sensor type (e.g. ld2450, pir)") }, singleLine = true)
            OutlinedTextField(room, { room = it }, label = { Text("Room") }, singleLine = true)
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(onClick = { model.approveNode(n.nodeId, name, type, room) },
                    enabled = name.isNotBlank() && type.isNotBlank() && room.isNotBlank()) { Text("Let it in") }
                OutlinedButton(onClick = { model.denyNode(n.nodeId) }) { Text("Deny") }
            }
        }
    }
}

@Composable
private fun Section(title: String) {
    Text(title, color = WavrTokens.text, fontSize = 21.sp, fontWeight = FontWeight.Bold,
        modifier = Modifier.padding(top = 8.dp))
}

/** A switch whose state may be unknown: then it shows "unknown" and cannot be flipped. */
@Composable
private fun Toggle(label: String, on: Boolean?, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().background(WavrTokens.surface).padding(14.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = WavrTokens.text, modifier = Modifier.weight(1f))
        if (on == null) Text("unknown", color = WavrTokens.dim)
        else Switch(checked = on, onCheckedChange = onChange)
    }
}
