// Wavr Desktop (Tauri v2) — a native window + tray around the loopback central. See
// ADR-0007 and docs/superpowers/specs/2026-07-03-tauri-desktop-shell-design.md.
//
// NOTE: this file was authored on a machine WITHOUT the Rust toolchain, so it is
// correct-by-convention against the Tauri v2 stable API and MUST be compiled + run once
// (`npm run tauri dev`) before this branch is merged. It adds no Python surface.
//
// What it does:
//   1. spawn  `python -m wavr.serve`  (loopback HTTP on 127.0.0.1:$WAVR_PORT, no LAN) as
//      a child process, remembered so we can kill it on quit,
//   2. poll   http://127.0.0.1:<port>/api/state  until it answers, then navigate the
//      window from the "Starting…" placeholder to the live dashboard,
//   3. tray:  Open Wavr / Quit; closing the window hides to tray (sensing keeps running),
//      Quit kills the backend child so the process exits and GPU VRAM is released.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WindowEvent};

const PORT_ENV: &str = "WAVR_PORT";
const DEFAULT_PORT: &str = "8000";

/// Holds the spawned backend so it can be killed on quit.
struct Backend(Mutex<Option<Child>>);

fn port() -> String {
    std::env::var(PORT_ENV).unwrap_or_else(|_| DEFAULT_PORT.to_string())
}

fn backend_url() -> String {
    format!("http://127.0.0.1:{}", port())
}

/// Resolve the Python interpreter: `WAVR_PYTHON`, else `python` on PATH. In dev, prefer
/// setting `WAVR_PYTHON` to the repo venv (…/.venv/Scripts/python.exe).
fn python() -> String {
    std::env::var("WAVR_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn spawn_backend() -> std::io::Result<Child> {
    let mut cmd = Command::new(python());
    cmd.args(["-m", "wavr.serve"]);
    // Run from the backend/repo dir if given, so the backend's load_dotenv() finds ./.env.
    if let Ok(dir) = std::env::var("WAVR_BACKEND_DIR") {
        cmd.current_dir(dir);
    }
    // Loopback mode: deliberately DO NOT set WAVR_MULTIDEVICE. Only the port is passed.
    cmd.env(PORT_ENV, port());
    cmd.spawn()
}

/// Block until the backend answers its readiness probe, or the timeout elapses.
fn wait_healthy(url: &str, timeout: Duration) -> bool {
    let probe = format!("{url}/api/state");
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if ureq::get(&probe).call().is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(400));
    }
    false
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Some(mut child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

fn main() {
    tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            // 1. spawn the backend and remember it for cleanup.
            match spawn_backend() {
                Ok(child) => {
                    *app.state::<Backend>().0.lock().unwrap() = Some(child);
                }
                Err(e) => eprintln!("failed to start Wavr backend: {e}"),
            }

            // 2. tray icon + menu.
            let open = MenuItemBuilder::with_id("open", "Open Wavr").build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
            let menu = MenuBuilder::new(app).items(&[&open, &quit]).build()?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Wavr Desktop")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => {
                        kill_backend(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            // 3. once the backend is healthy, navigate the window to it. Done off-thread so
            //    setup() returns immediately and the "Starting…" placeholder shows meanwhile.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let url = backend_url();
                if wait_healthy(&url, Duration::from_secs(20)) {
                    if let (Some(w), Ok(u)) =
                        (handle.get_webview_window("main"), url.parse::<tauri::Url>())
                    {
                        let _ = w.navigate(u);
                    }
                } else {
                    eprintln!("Wavr backend did not become healthy at {url}");
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window hides to tray; the central keeps sensing.
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .build(tauri::generate_context!())
        .expect("error building Wavr Desktop")
        .run(|app, event| {
            // Kill the backend if the app exits by any path (belt-and-suspenders).
            if let RunEvent::ExitRequested { .. } = event {
                kill_backend(app);
            }
        });
}
