mod ffi;
mod snapshot;
mod theme;

use slint::{Brush, Color, ComponentHandle, ModelRc, VecModel};
use std::time::Duration;

slint::slint! {
    import { ScrollView } from "std-widgets.slint";

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

        background: bg;
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

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let (url, pin) = arguments()?;
    let native = ffi::NativeRuntime::load()?;
    let token = std::env::var("WAVR_LOCAL_TOKEN").ok();
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
    let weak = ui.as_weak();
    std::thread::spawn(move || loop {
        let result = native.fetch(&url, token.as_deref(), pin.as_deref())
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
