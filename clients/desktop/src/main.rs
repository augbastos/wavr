mod ffi;
mod snapshot;
mod theme;

use serde_json::{Value, json};
use slint::{Brush, Color, ComponentHandle, ModelRc, SharedString, VecModel};
use std::sync::Arc;
use std::time::Duration;

slint::slint! {
    import { ScrollView, Switch, LineEdit, Button, CheckBox, Palette } from "std-widgets.slint";

    struct ToggleRow { key: string, label: string, on: bool, known: bool }
    struct PairingRow { id: string, title: string }
    struct NodeRow { id: string, hint: string }

    struct DisplayRow {
        title: string,
        status: string,
        detail: string,
        source: bool,
    }

    export component WavrWindow inherits Window {
        title: "Wavr";
        preferred-width: 900px;
        preferred-height: 680px;
        min-width: 620px;
        min-height: 480px;

        in property <brush> bg;
        in property <brush> surface;
        in property <brush> elevated;
        in property <brush> text-color;
        in property <brush> dim-color;
        in property <brush> state-color;
        in property <string> space-name: "Unknown space";
        in property <string> state-name: "Unknown";
        in property <string> headline: "Waiting for Wavr";
        in property <string> attention-headline: "Attention unknown";
        in property <string> device-text: "Device data unknown";
        in property <[DisplayRow]> room-lines;
        in property <[DisplayRow]> attention-lines;
        in-out property <int> tab: 0;
        // Manage: every action is one command of the native command contract.
        in property <[ToggleRow]> toggles;
        in property <[PairingRow]> pairings;
        in property <[NodeRow]> nodes;
        in property <[DisplayRow]> doctor-lines;
        in property <bool> on-core;
        in property <string> manage-message;
        callback toggle(string, bool);
        callback approve-pairing(string, string, bool);
        callback deny-pairing(string);
        callback approve-node(string, string, string, string);
        callback deny-node(string);
        callback run-doctor();
        callback open-manage();

        background: bg;
        // The standard widgets default to the light scheme: dark text on this dark
        // window. Same defect the Android theme fixed; same fix, from the tokens' side.
        init => { Palette.color-scheme = ColorScheme.dark; }
        VerticalLayout {
            padding: 22px;
            spacing: 16px;
            Rectangle {
                background: surface;
                border-radius: 10px;
                height: 112px;
                VerticalLayout {
                    padding: 16px;
                    spacing: 5px;
                    HorizontalLayout {
                        spacing: 12px;
                        Text { text: "WAVR / " + root.space-name; color: root.text-color; font-size: 22px; font-weight: 700; accessible-role: text; accessible-label: self.text; }
                        Rectangle { width: 12px; height: 12px; border-radius: 6px; background: root.state-color; }
                        Text { text: root.state-name; color: root.state-color; font-size: 15px; vertical-alignment: center; accessible-role: text; accessible-label: self.text; accessible-live-region: polite; }
                    }
                    Text { text: root.headline; color: root.dim-color; font-size: 15px; accessible-role: text; accessible-label: self.text; }
                }
            }
            HorizontalLayout {
                spacing: 10px;
                Rectangle {
                    width: 150px; height: 42px; border-radius: 7px;
                    background: root.tab == 0 ? root.elevated : root.surface;
                    Text { text: "Overview"; color: root.text-color; horizontal-alignment: center; vertical-alignment: center; }
                    TouchArea { clicked => { root.tab = 0; } accessible-role: AccessibleRole.tab; accessible-label: "Overview"; }
                }
                Rectangle {
                    width: 150px; height: 42px; border-radius: 7px;
                    background: root.tab == 2 ? root.elevated : root.surface;
                    Text { text: "Manage"; color: root.text-color; horizontal-alignment: center; vertical-alignment: center; }
                    TouchArea { clicked => { root.tab = 2; root.open-manage(); } accessible-role: AccessibleRole.tab; accessible-label: "Manage"; }
                }
                Rectangle {
                    width: 150px; height: 42px; border-radius: 7px;
                    background: root.tab == 1 ? root.elevated : root.surface;
                    Text { text: "Device"; color: root.text-color; horizontal-alignment: center; vertical-alignment: center; }
                    TouchArea { clicked => { root.tab = 1; } accessible-role: AccessibleRole.tab; accessible-label: "Device"; }
                }
            }
            if root.tab == 0 : HorizontalLayout {
                spacing: 16px;
                Rectangle {
                    background: root.surface;
                    border-radius: 10px;
                    horizontal-stretch: 3;
                    VerticalLayout {
                        padding: 14px;
                        spacing: 10px;
                        Text { text: "ROOMS & SOURCES"; color: root.dim-color; font-size: 13px; font-weight: 700; accessible-role: text; accessible-label: self.text; }
                        ScrollView {
                            vertical-stretch: 1;
                            VerticalLayout {
                                spacing: 8px;
                                for row in root.room-lines : Rectangle {
                                    background: root.elevated;
                                    border-radius: 6px;
                                    height: row.source ? 72px : 96px;
                                    accessible-role: list-item;
                                    accessible-label: row.title + ". " + row.status + ". " + row.detail;
                                    VerticalLayout {
                                        padding: 11px;
                                        spacing: 3px;
                                        Text { text: row.title; color: row.source ? root.dim-color : root.text-color; font-size: row.source ? 13px : 17px; font-weight: row.source ? 400 : 700; }
                                        Text { text: row.status; color: root.text-color; font-size: 13px; }
                                        Text { text: row.detail; color: root.dim-color; font-size: 12px; overflow: elide; }
                                    }
                                }
                            }
                        }
                    }
                }
                Rectangle {
                    background: root.surface;
                    border-radius: 10px;
                    horizontal-stretch: 2;
                    VerticalLayout {
                        padding: 14px;
                        spacing: 10px;
                        Text { text: "ATTENTION"; color: root.dim-color; font-size: 13px; font-weight: 700; accessible-role: text; accessible-label: self.text; }
                        Text { text: root.attention-headline; color: root.text-color; font-size: 14px; wrap: word-wrap; accessible-role: text; accessible-label: self.text; accessible-live-region: polite; }
                        ScrollView {
                            vertical-stretch: 1;
                            VerticalLayout {
                                spacing: 8px;
                                for row in root.attention-lines : Rectangle {
                                    background: root.elevated;
                                    border-radius: 6px;
                                    height: 90px;
                                    accessible-role: list-item;
                                    accessible-label: row.title + ". " + row.status + ". " + row.detail;
                                    VerticalLayout {
                                        padding: 10px;
                                        spacing: 3px;
                                        Text { text: row.title; color: root.text-color; font-size: 15px; font-weight: 700; overflow: elide; }
                                        Text { text: row.status; color: root.dim-color; font-size: 12px; }
                                        Text { text: row.detail; color: root.dim-color; font-size: 12px; overflow: elide; }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if root.tab == 2 : Rectangle {
                background: root.surface;
                border-radius: 10px;
                ScrollView {
                    VerticalLayout {
                        padding: 18px;
                        spacing: 10px;
                        alignment: start;
                        if root.manage-message != "" : Text { text: root.manage-message; color: root.text-color; font-size: 14px; wrap: word-wrap; accessible-role: text; accessible-label: self.text; accessible-live-region: polite; }
                        Text { text: "PRIVACY AND SENSING"; color: root.dim-color; font-size: 13px; font-weight: 700; }
                        for t in root.toggles : HorizontalLayout {
                            spacing: 12px;
                            height: 36px;
                            Text { text: t.label; color: root.text-color; font-size: 14px; vertical-alignment: center; horizontal-stretch: 1; }
                            if t.known : Switch { checked: t.on; toggled => { root.toggle(t.key, self.checked); } accessible-label: t.label; }
                            if !t.known : Text { text: "Unknown"; color: root.dim-color; vertical-alignment: center; }
                        }
                        if root.on-core : Text { text: "DEVICES ASKING TO JOIN"; color: root.dim-color; font-size: 13px; font-weight: 700; }
                        if root.on-core && root.pairings.length == 0 : Text { text: "None"; color: root.dim-color; }
                        if root.on-core : VerticalLayout {
                            spacing: 8px;
                            for p in root.pairings : Rectangle {
                                background: root.elevated;
                                border-radius: 6px;
                                VerticalLayout {
                                    padding: 10px;
                                    spacing: 6px;
                                    Text { text: p.title; color: root.text-color; font-size: 15px; }
                                    code := LineEdit { placeholder-text: "Code shown on that device"; accessible-label: "Code shown on that device"; }
                                    central := CheckBox { text: "May change settings (central)"; }
                                    HorizontalLayout {
                                        spacing: 8px;
                                        alignment: start;
                                        Button { text: "Let it in"; enabled: code.text != ""; clicked => { root.approve-pairing(p.id, code.text, central.checked); } }
                                        Button { text: "Deny"; clicked => { root.deny-pairing(p.id); } }
                                    }
                                }
                            }
                        }
                        if root.on-core : Text { text: "SENSORS ASKING TO JOIN"; color: root.dim-color; font-size: 13px; font-weight: 700; }
                        if root.on-core && root.nodes.length == 0 : Text { text: "None"; color: root.dim-color; }
                        if root.on-core : VerticalLayout {
                            spacing: 8px;
                            for n in root.nodes : Rectangle {
                                background: root.elevated;
                                border-radius: 6px;
                                VerticalLayout {
                                    padding: 10px;
                                    spacing: 6px;
                                    // What the sensor says about itself is a claim: a hint, never trusted.
                                    Text { text: "It says: " + n.hint; color: root.dim-color; font-size: 13px; }
                                    name := LineEdit { placeholder-text: "Name"; }
                                    kind := LineEdit { placeholder-text: "Sensor type (e.g. ld2450, pir)"; }
                                    room := LineEdit { placeholder-text: "Room"; }
                                    HorizontalLayout {
                                        spacing: 8px;
                                        alignment: start;
                                        Button { text: "Let it in"; enabled: name.text != "" && kind.text != "" && room.text != ""; clicked => { root.approve-node(n.id, name.text, kind.text, room.text); } }
                                        Button { text: "Deny"; clicked => { root.deny-node(n.id); } }
                                    }
                                }
                            }
                        }
                        Text { text: "DIAGNOSIS"; color: root.dim-color; font-size: 13px; font-weight: 700; }
                        HorizontalLayout { alignment: start; Button { text: "Run the Core's checks"; clicked => { root.run-doctor(); } } }
                        for row in root.doctor-lines : Rectangle {
                            background: root.elevated;
                            border-radius: 6px;
                            height: 58px;
                            accessible-role: list-item;
                            accessible-label: row.title + ". " + row.status + ". " + row.detail;
                            VerticalLayout {
                                padding: 8px;
                                Text { text: row.title + "  ·  " + row.status; color: root.text-color; font-size: 13px; }
                                Text { text: row.detail; color: root.dim-color; font-size: 12px; overflow: elide; }
                            }
                        }
                    }
                }
            }
            if root.tab == 1 : Rectangle {
                background: root.surface;
                border-radius: 10px;
                VerticalLayout {
                    padding: 18px;
                    spacing: 12px;
                    Text { text: "DEVICE CAPABILITIES"; color: root.dim-color; font-size: 13px; font-weight: 700; accessible-role: text; accessible-label: self.text; }
                    ScrollView {
                        vertical-stretch: 1;
                        Text { text: root.device-text; color: root.text-color; font-size: 13px; wrap: word-wrap; accessible-role: text; accessible-label: self.text; }
                    }
                }
            }
        }
    }
}

fn brush(hex: &str) -> Brush {
    let hex = hex.strip_prefix('#').expect("static token starts with #");
    let rgb = u32::from_str_radix(hex, 16).expect("valid static design token");
    Brush::SolidColor(Color::from_rgb_u8((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8))
}

fn known(value: Option<&str>) -> &str { value.unwrap_or("Unknown") }
fn yes_no(value: Option<bool>) -> &'static str {
    match value { Some(true) => "Yes", Some(false) => "No", None => "Unknown" }
}
fn count(value: Option<u64>) -> String { value.map_or_else(|| "Unknown".into(), |v| v.to_string()) }
fn seconds(value: Option<f64>) -> String { value.map_or_else(|| "Unknown".into(), |v| format!("{v:.1}s")) }
fn confidence(value: Option<f64>) -> String {
    value.filter(|v| (0.0..=1.0).contains(v)).map_or_else(|| "Unknown".into(), |v| format!("{:.0}%", v * 100.0))
}

fn rows(s: &snapshot::Snapshot) -> (Vec<DisplayRow>, Vec<DisplayRow>, String) {
    let mut rooms = Vec::new();
    if s.rooms_readable != Some(true) {
        rooms.push(DisplayRow { title: "Rooms unavailable".into(), status: "Unknown".into(), detail: "Wavr could not read room state".into(), source: false });
    } else if let Some(items) = &s.rooms {
        for room in items {
            rooms.push(DisplayRow {
                title: known(room.room.as_deref()).into(),
                status: format!("Occupied: {}  ·  Confidence: {}  ·  People: {}", yes_no(room.occupied), confidence(room.confidence), count(room.person_count)).into(),
                detail: format!("Precision: {}  ·  {}", known(room.precision_level.as_deref()), known(room.explanation.as_deref())).into(),
                source: false,
            });
            if let Some(sources) = &room.sources {
                for source in sources {
                    rooms.push(DisplayRow {
                        title: format!("↳ {} / {}", known(source.modality.as_deref()), known(source.sensor_id.as_deref())).into(),
                        status: format!("Health: {}  ·  Age: {}", known(source.health.as_deref()), seconds(source.age_s)).into(),
                        detail: format!("Presence: {}  ·  Confidence: {}  ·  Count: {}", yes_no(source.presence), confidence(source.confidence), count(source.count)).into(),
                        source: true,
                    });
                }
            } else {
                rooms.push(DisplayRow { title: "↳ Sources unknown".into(), status: "Unknown".into(), detail: "Source evidence unavailable".into(), source: true });
            }
        }
        if items.is_empty() {
            rooms.push(DisplayRow { title: "No rooms reported".into(), status: "Unknown occupancy".into(), detail: "No room state in snapshot".into(), source: false });
        }
    } else {
        rooms.push(DisplayRow { title: "Rooms unknown".into(), status: "Unknown occupancy".into(), detail: "Room list unavailable".into(), source: false });
    }
    let mut attention = Vec::new();
    let headline = if let Some(a) = &s.attention {
        if let Some(items) = &a.items {
            for item in items {
                attention.push(DisplayRow {
                    title: known(item.title.as_deref()).into(),
                    status: format!("{}  ·  {}", known(item.band.as_deref()), known(item.r#where.as_deref())).into(),
                    detail: known(item.detail.as_deref()).into(),
                    source: false,
                });
            }
        } else {
            attention.push(DisplayRow { title: "Attention items unknown".into(), status: "Unknown".into(), detail: "Item list unavailable".into(), source: false });
        }
        if let Some(checks) = &a.could_not_check {
            for check in checks {
                attention.push(DisplayRow { title: format!("Could not check: {check}").into(), status: "Unknown".into(), detail: "This check did not complete".into(), source: false });
            }
        } else {
            attention.push(DisplayRow { title: "Could not check: unknown".into(), status: "Unknown".into(), detail: "Check list unavailable".into(), source: false });
        }
        a.headline.clone().unwrap_or_else(|| "Attention unknown".into())
    } else {
        attention.push(DisplayRow { title: "Attention unavailable".into(), status: "Unknown".into(), detail: "Wavr did not report attention state".into(), source: false });
        "Attention unknown".into()
    };
    (rooms, attention, headline)
}

fn apply_snapshot(ui: &WavrWindow, s: snapshot::Snapshot) {
    let runtime = s.runtime.as_ref();
    let state = if s.reachable == Some(true) { runtime.and_then(|v| v.state.as_deref()) } else { None };
    ui.set_space_name(runtime.and_then(|v| v.space.as_deref()).unwrap_or("Unknown space").into());
    ui.set_state_name(known(state).into());
    ui.set_state_color(brush(theme::state_color(state)));
    ui.set_headline(s.error.as_deref().or_else(|| runtime.and_then(|v| v.headline.as_deref())).unwrap_or("Runtime state unknown").into());
    let (rooms, attention, headline) = rows(&s);
    ui.set_room_lines(ModelRc::new(VecModel::from(rooms)));
    ui.set_attention_lines(ModelRc::new(VecModel::from(attention)));
    ui.set_attention_headline(headline.into());
}

fn arguments() -> Result<(String, Option<String>), String> {
    let mut url = "http://127.0.0.1:8000".to_owned();
    let mut pin = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--url" => url = args.next().ok_or("--url needs a value")?,
            "--pin" => pin = Some(args.next().ok_or("--pin needs a value")?),
            "--help" | "-h" => return Err("Usage: wavr-desktop-native [--url URL] [--pin PIN]".into()),
            _ => return Err(format!("Unknown argument: {arg}")),
        }
    }
    Ok((url, pin))
}

/// Which Core, and how this window may speak to it.
#[derive(Clone)]
struct Conn {
    url: String,
    token: Option<String>,
    pin: Option<String>,
}

impl Conn {
    /// The Core's own screen: approvals of devices and sensors live only there.
    fn on_core(&self) -> bool {
        let host = self.url.split("://").nth(1).unwrap_or("").split(['/', ':']).next().unwrap_or("");
        host == "127.0.0.1" || host == "localhost"
    }
}

/// One command of the native command contract; the reply JSON, or a synthetic
/// "unreachable" reply if the library could not produce one.
fn command(native: &ffi::NativeRuntime, c: &Conn, name: &str, args: Value) -> Value {
    native.command(&c.url, c.token.as_deref(), c.pin.as_deref(), name, &args.to_string())
        .ok().and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_else(|| json!({"ok": false, "error": "unreachable", "detail": "the native runtime returned nothing"}))
}

/// One sentence for a person; the Core's own words when it gave some.
fn explain(reply: &Value, done: &str) -> String {
    if reply["ok"] == true { return done.to_owned(); }
    let why = reply["detail"].as_str().map(|d| format!(": {d}")).unwrap_or_else(|| ".".into());
    let what = match reply["error"].as_str() {
        Some("forbidden") => "Not allowed from this device",
        Some("unauthorized") => "This device's credential was refused",
        Some("unreachable") => "The Core did not answer",
        Some("not_found") => "The Core does not know that (any more)",
        Some("throttled") => "Too many attempts; wait a minute",
        Some("server") => "The Core failed while doing it",
        Some("bad_call") => "This window sent a malformed request",
        _ => "The Core refused it",
    };
    format!("{what}{why}")
}

/// The Manage tab's rows, read from the Core. Everything unreadable stays unknown.
struct ManageView {
    toggles: Vec<ToggleRow>,
    pairings: Vec<PairingRow>,
    nodes: Vec<NodeRow>,
    message: String,
}

fn read_manage(native: &ffi::NativeRuntime, c: &Conn) -> ManageView {
    let watch = command(native, c, "watch.get", json!({}));
    let system = command(native, c, "sources.list", json!({}));
    let toggle = |key: &str, label: &str, v: &Value| ToggleRow {
        key: key.into(), label: label.into(), on: v.as_bool().unwrap_or(false), known: v.is_boolean() };
    let mut toggles = vec![
        toggle("watch", "Watch (flag people Wavr does not recognise)", &watch["data"]["on"]),
        toggle("sensing", "Sensing", &system["data"]["running"]),
    ];
    if let Some(sources) = system["data"]["sources"].as_array() {
        for src in sources {
            if let Some(name) = src["name"].as_str() {
                let state = src["state"].as_str().unwrap_or("unknown");
                toggles.push(toggle(name, &format!("{name}  ·  {state}"), &src["enabled"]));
            }
        }
    }
    let (mut pairings, mut nodes) = (Vec::new(), Vec::new());
    let mut replies = vec![watch, system];
    if c.on_core() {
        let p = command(native, c, "pairings.list", json!({}));
        for r in p["data"]["requests"].as_array().into_iter().flatten() {
            if let Some(id) = r["request_id"].as_str() {
                let title = format!("{}  ·  {}", r["requester_name"].as_str().unwrap_or("Unknown"),
                                    r["platform"].as_str().unwrap_or("unknown"));
                pairings.push(PairingRow { id: id.into(), title: title.into() });
            }
        }
        let n = command(native, c, "nodes.pending", json!({}));
        for r in n["data"]["pending"].as_array().into_iter().flatten() {
            if let Some(id) = r["node_id"].as_str() {
                let hint = format!("{}  ·  {}", r["name_hint"].as_str().unwrap_or("unknown"),
                                   r["sensor_hint"].as_str().unwrap_or("unknown"));
                nodes.push(NodeRow { id: id.into(), hint: hint.into() });
            }
        }
        replies.push(p);
        replies.push(n);
    }
    // The first refusal says why a section is empty (e.g. a 'user' device).
    let message = replies.iter().find(|r| r["ok"] != true).map(|r| explain(r, "")).unwrap_or_default();
    ManageView { toggles, pairings, nodes, message }
}

fn apply_manage(ui: &WavrWindow, view: ManageView, note: Option<String>) {
    ui.set_toggles(ModelRc::new(VecModel::from(view.toggles)));
    ui.set_pairings(ModelRc::new(VecModel::from(view.pairings)));
    ui.set_nodes(ModelRc::new(VecModel::from(view.nodes)));
    ui.set_manage_message(note.filter(|n| !n.is_empty()).unwrap_or(view.message).into());
}

/// Run one command off the UI thread, then re-read the Manage tab.
fn act(weak: slint::Weak<WavrWindow>, native: Arc<ffi::NativeRuntime>, c: Arc<Conn>,
       name: &'static str, args: Value, done: String) {
    std::thread::spawn(move || {
        let reply = command(&native, &c, name, args);
        let note = explain(&reply, &done);
        let view = read_manage(&native, &c);
        let _ = weak.upgrade_in_event_loop(move |ui| apply_manage(&ui, view, Some(note)));
    });
}

fn doctor_rows(reply: &Value) -> Vec<DisplayRow> {
    let Some(checks) = reply["data"]["checks"].as_array() else {
        return vec![DisplayRow { title: "Checks unavailable".into(), status: "Unknown".into(),
                                 detail: explain(reply, "").into(), source: false }];
    };
    checks.iter().map(|c| DisplayRow {
        title: c["id"].as_str().unwrap_or("unknown").into(),
        // ok = null is the Core's honest "not applicable", kept as such.
        status: match c["ok"].as_bool() { Some(true) => "ok".into(), Some(false) => c["severity"].as_str().unwrap_or("problem").into(), None => "not applicable".into() },
        detail: c["detail"].as_str().unwrap_or("").into(),
        source: false,
    }).collect()
}

fn wire_manage(ui: &WavrWindow, native: &Arc<ffi::NativeRuntime>, conn: &Arc<Conn>) {
    ui.set_on_core(conn.on_core());
    let (w, n, c) = (ui.as_weak(), native.clone(), conn.clone());
    ui.on_open_manage(move || {
        let (w, n, c) = (w.clone(), n.clone(), c.clone());
        std::thread::spawn(move || {
            let view = read_manage(&n, &c);
            let _ = w.upgrade_in_event_loop(move |ui| apply_manage(&ui, view, None));
        });
    });
    let (w, n, c) = (ui.as_weak(), native.clone(), conn.clone());
    ui.on_toggle(move |key: SharedString, on: bool| {
        let (name, args) = match key.as_str() {
            "watch" => ("watch.set", json!({"on": on})),
            "sensing" => ("sensing.set", json!({"on": on})),
            source => ("source.set", json!({"name": source, "enabled": on})),
        };
        act(w.clone(), n.clone(), c.clone(), name, args, format!("{key} {}.", if on { "on" } else { "off" }));
    });
    let (w, n, c) = (ui.as_weak(), native.clone(), conn.clone());
    ui.on_approve_pairing(move |id: SharedString, code: SharedString, central: bool| {
        let role = if central { "central" } else { "user" };
        let digits: String = code.chars().filter(char::is_ascii_digit).collect();
        act(w.clone(), n.clone(), c.clone(), "pairings.approve",
            json!({"id": id.as_str(), "confirm_code": digits, "role": role}), format!("Device let in as {role}."));
    });
    let (w, n, c) = (ui.as_weak(), native.clone(), conn.clone());
    ui.on_deny_pairing(move |id: SharedString| {
        act(w.clone(), n.clone(), c.clone(), "pairings.deny", json!({"id": id.as_str()}), "Request denied.".into());
    });
    let (w, n, c) = (ui.as_weak(), native.clone(), conn.clone());
    ui.on_approve_node(move |id: SharedString, name: SharedString, kind: SharedString, room: SharedString| {
        act(w.clone(), n.clone(), c.clone(), "node.approve",
            json!({"id": id.as_str(), "name": name.as_str(), "sensor_type": kind.as_str(), "room": room.as_str()}),
            "Sensor let in.".into());
    });
    let (w, n, c) = (ui.as_weak(), native.clone(), conn.clone());
    ui.on_deny_node(move |id: SharedString| {
        act(w.clone(), n.clone(), c.clone(), "node.deny", json!({"id": id.as_str()}), "Sensor denied.".into());
    });
    let (w, n, c) = (ui.as_weak(), native.clone(), conn.clone());
    ui.on_run_doctor(move || {
        let (w, n, c) = (w.clone(), n.clone(), c.clone());
        std::thread::spawn(move || {
            let rows = doctor_rows(&command(&n, &c, "doctor.run", json!({})));
            let _ = w.upgrade_in_event_loop(move |ui| ui.set_doctor_lines(ModelRc::new(VecModel::from(rows))));
        });
    });
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let (url, pin) = arguments()?;
    let native = Arc::new(ffi::NativeRuntime::load()?);
    let conn = Arc::new(Conn { url, token: std::env::var("WAVR_LOCAL_TOKEN").ok(), pin });
    let ui = WavrWindow::new()?;
    ui.set_bg(brush(theme::BG));
    ui.set_surface(brush(theme::SURFACE));
    ui.set_elevated(brush(theme::ELEVATED));
    ui.set_text_color(brush(theme::TEXT));
    ui.set_dim_color(brush(theme::DIM));
    ui.set_state_color(brush(theme::UNKNOWN));   // nothing read yet: unknown, not "down"
    let device = native.capability_manifest().and_then(|json| {
        serde_json::from_str::<serde_json::Value>(&json)
            .map_err(|e| format!("Invalid capability manifest: {e}"))
            .and_then(|v| serde_json::to_string_pretty(&v).map_err(|e| e.to_string()))
    }).unwrap_or_else(|e| format!("Device capabilities unknown: {e}"));
    ui.set_device_text(format!("Native runtime: {}\n\n{device}", native.version().unwrap_or_else(|| "Unknown".into())).into());
    wire_manage(&ui, &native, &conn);
    let weak = ui.as_weak();
    let (native, conn) = (native.clone(), conn.clone());
    std::thread::spawn(move || loop {
        let result = native.fetch(&conn.url, conn.token.as_deref(), conn.pin.as_deref())
            .and_then(|handle| {
                let json = handle.json()?;
                let _native_exit_code = handle.exit_code();
                snapshot::Snapshot::parse(&json).map_err(|e| format!("Invalid snapshot JSON: {e}"))
            });
        if weak.upgrade_in_event_loop(move |ui| match result {
            Ok(snapshot) => apply_snapshot(&ui, snapshot),
            Err(error) => {
                apply_snapshot(&ui, snapshot::Snapshot { error: Some(error), ..Default::default() });
            }
        }).is_err() { break; }
        std::thread::sleep(Duration::from_secs(5));
    });
    ui.run()?;
    Ok(())
}

/// The window drawn by Slint's software renderer, with no display at all: the
/// same component and the same `apply_snapshot` the app uses, fed the canonical
/// snapshots from conformance/client_view.json. Screenshots land in
/// target/render/ for a person to look at; the assertions are about colour,
/// because colour is how the window says "healthy" -- and a Core that did not
/// answer must never be painted in it.
#[cfg(test)]
mod render {
    use super::*;
    use slint::Rgb8Pixel;
    use slint::platform::software_renderer::{MinimalSoftwareWindow, RepaintBufferType};
    use slint::platform::{Platform, WindowAdapter};
    use std::rc::Rc;

    struct Headless(Rc<MinimalSoftwareWindow>);
    impl Platform for Headless {
        fn create_window_adapter(&self) -> Result<Rc<dyn WindowAdapter>, slint::PlatformError> {
            Ok(self.0.clone())
        }
    }

    const W: u32 = 900;
    const H: u32 = 680;

    fn case(name: &str) -> snapshot::Snapshot {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../conformance/client_view.json");
        let fx: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let c = fx["cases"].as_array().unwrap().iter().find(|c| c["name"] == name).unwrap();
        snapshot::Snapshot::parse(&c["snapshot"].to_string()).unwrap()
    }

    fn frame(window: &MinimalSoftwareWindow, name: &str) -> Vec<Rgb8Pixel> {
        slint::platform::update_timers_and_animations();
        let mut buf = vec![Rgb8Pixel::default(); (W * H) as usize];
        window.draw_if_needed(|r| { r.render(&mut buf, W as usize); });
        let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/target/render");
        std::fs::create_dir_all(dir).unwrap();
        let file = std::fs::File::create(format!("{dir}/{name}.png")).unwrap();
        let mut enc = png::Encoder::new(std::io::BufWriter::new(file), W, H);
        enc.set_color(png::ColorType::Rgb);
        let bytes: Vec<u8> = buf.iter().flat_map(|p| [p.r, p.g, p.b]).collect();
        enc.write_header().unwrap().write_image_data(&bytes).unwrap();
        buf
    }

    fn painted(buf: &[Rgb8Pixel], hex: &str) -> usize {
        let v = u32::from_str_radix(hex.trim_start_matches('#'), 16).unwrap();
        let (r, g, b) = ((v >> 16) as u8, (v >> 8) as u8, v as u8);
        buf.iter().filter(|p| p.r == r && p.g == g && p.b == b).count()
    }

    #[test]
    fn every_state_renders_without_a_display_and_only_healthy_is_green() {
        let window = MinimalSoftwareWindow::new(RepaintBufferType::NewBuffer);
        slint::platform::set_platform(Box::new(Headless(window.clone()))).unwrap();
        window.set_size(slint::PhysicalSize::new(W, H));
        let ui = WavrWindow::new().unwrap();
        ui.set_bg(brush(theme::BG));
        ui.set_surface(brush(theme::SURFACE));
        ui.set_elevated(brush(theme::ELEVATED));
        ui.set_text_color(brush(theme::TEXT));
        ui.set_dim_color(brush(theme::DIM));
        ui.show().unwrap();
        let green = theme::state_color(Some("healthy"));

        apply_snapshot(&ui, case("healthy_empty_inbox"));
        let healthy = frame(&window, "overview-healthy");
        assert!(painted(&healthy, theme::BG) > 10_000, "the window background was drawn");
        assert!(painted(&healthy, green) > 40, "a healthy Core is shown in the healthy colour");

        apply_snapshot(&ui, case("core_not_answering"));
        let down = frame(&window, "overview-core-not-answering");
        assert_eq!(painted(&down, green), 0, "a Core that did not answer is never painted healthy");

        ui.set_tab(2);
        ui.set_on_core(true);
        ui.set_toggles(ModelRc::new(VecModel::from(vec![
            ToggleRow { key: "watch".into(), label: "Watch".into(), on: true, known: true },
            ToggleRow { key: "sensing".into(), label: "Sensing".into(), on: false, known: false },
        ])));
        ui.set_pairings(ModelRc::new(VecModel::from(vec![PairingRow { id: "r1".into(), title: "Phone  ·  android".into() }])));
        ui.set_manage_message("The Core refused it: central role required".into());
        let manage = frame(&window, "manage");
        assert!(painted(&manage, theme::ELEVATED) > 1_000, "the Manage tab draws its pending-device card");
    }
}
