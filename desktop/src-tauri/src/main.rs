// Wavr Desktop (Tauri v2) — a native window + tray around the Wavr central. See
// ADR-0007 and docs/superpowers/specs/2026-07-03-tauri-desktop-shell-design.md.
//
// This shell is MULTIDEVICE-AWARE (ADR-0006). It reads the SAME effective config the
// backend reads (process env, else `WAVR_BACKEND_DIR/.env`, mirroring python-dotenv's
// override=False) and adapts:
//
//   * WAVR_MULTIDEVICE off (default)  -> backend is plain HTTP on 127.0.0.1. Probe/webview
//     over http, exactly as before.
//   * WAVR_MULTIDEVICE on             -> the desktop is the LAN central: the backend binds
//     HTTPS/WSS on WAVR_BIND (e.g. 0.0.0.0) with a self-signed local cert (wavr/tls.py,
//     SANs localhost/127.0.0.1/<LAN-IP>). This shell still talks ONLY to the LOOPBACK
//     side of it: https://127.0.0.1:<port>. Nothing here ever reaches a non-loopback host.
//
// Local-only invariant, upheld for the HTTPS path WITHOUT trust-all:
//   * The readiness probe uses a rustls client whose certificate verifier PINS the exact
//     backend cert (DER byte-equality against the on-disk cert.pem) — never
//     danger_accept_invalid_certs. A wrong/substituted cert fails the handshake.
//   * On Windows, WebView2 rejects the self-signed cert by default. We handle
//     ServerCertificateErrorDetected and set AlwaysAllow ONLY when BOTH (a) the request
//     authority is exactly 127.0.0.1:<port> AND (b) the presented cert is byte-identical
//     to the on-disk backend cert. Anything else is CANCELLED. This is scoped pinning, not
//     a global --ignore-certificate-errors.
//
// A note on `tauri.conf.json`'s `app.security.csp` (kept out of the JSON file itself: it
// is parsed as strict JSON here -- no `config-json5` feature is enabled -- so a `//`
// comment there would fail the build): `connect-src`/`img-src` currently allow
// `http(s)://127.0.0.1:*` / `ws(s)://127.0.0.1:*` (any loopback port, not just `port()`'s
// value) because `script-src` still carries `'unsafe-inline'`, which already lets any
// inline script reach any origin `connect-src` allows -- narrowing the port alone would be
// a false sense of restriction while `'unsafe-inline'` stands. If `'unsafe-inline'` is ever
// removed (e.g. once the dashboard's inline `<script>` is hashed/nonced), narrow those two
// rules to the single resolved port (`{scheme}://127.0.0.1:{port}` /
// `{ws-scheme}://127.0.0.1:{port}`) at the same time -- that pairing is what actually
// closes off any other loopback listener on the box.
//
// What it does:
//   1. spawn  `python -m wavr.serve`  (from WAVR_BACKEND_DIR so its load_dotenv() finds
//      ./.env) as a child process, remembered so we can kill it on quit,
//   2. (HTTPS mode, Windows) install the scoped WebView2 cert pin BEFORE any navigation,
//   3. poll   <scheme>://127.0.0.1:<port>/api/state  until it answers, then navigate the
//      window from the "Starting…" placeholder to the live dashboard,
//   4. tray:  Open Wavr / Quit; closing the window hides to tray (sensing keeps running),
//      Quit kills the backend child so the process exits and GPU VRAM is released.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, SignatureScheme};

use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_notification::NotificationExt;

const PORT_ENV: &str = "WAVR_PORT";
const DEFAULT_PORT: &str = "8000";

/// How long `setup()`'s background thread waits for the backend's readiness probe before
/// giving up (item 3). Named so the timeout UX's own message can quote the same number
/// `wait_healthy()` is actually called with.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(20);

/// Opt-out (default ON) for the native-OS-notification poller (item 4): set to
/// 0/false/no/off to silence it. Unlike every other `WAVR_*` var here this is a shell-only
/// setting -- the backend has no notion of it.
const NOTIFY_ENV: &str = "WAVR_DESKTOP_NOTIFICATIONS";
/// Opt-in (default OFF) launch-on-login (item 5). Reconciled declaratively every launch,
/// same "env var is the one source of truth" pattern as `WAVR_MULTIDEVICE` -- no UI/IPC
/// toggle exists or is added for this.
const AUTOSTART_ENV: &str = "WAVR_DESKTOP_AUTOSTART";
/// CLI arg the autostart plugin appends when IT launches this exe (see `main()`'s
/// `tauri_plugin_autostart::init` call). Its presence, and only its presence, is what
/// tells `setup()` this particular launch should start hidden to tray.
const AUTOSTART_ARG: &str = "--autostart";

/// Holds the spawned backend so it can be killed on quit.
struct Backend(Mutex<Option<Child>>);

// ---------------------------------------------------------------------------
// Effective config: process env wins, else the backend's ./.env (dotenv override=False).
// This is the faithful mirror of what `wavr.config.load_config()` sees, so the shell and
// the backend agree on mode/port/cert WITHOUT the shell setting any of them itself.
// ---------------------------------------------------------------------------

fn load_dotenv_map() -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Ok(dir) = std::env::var("WAVR_BACKEND_DIR") else {
        return map;
    };
    let Ok(text) = std::fs::read_to_string(Path::new(&dir).join(".env")) else {
        return map;
    };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        // Tolerate a leading `export `, like a shell would.
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let mut value = value.trim();
        // Strip one layer of matching surrounding quotes.
        if value.len() >= 2
            && ((value.starts_with('"') && value.ends_with('"'))
                || (value.starts_with('\'') && value.ends_with('\'')))
        {
            value = &value[1..value.len() - 1];
        }
        map.insert(key.to_string(), value.to_string());
    }
    map
}

fn dotenv() -> &'static HashMap<String, String> {
    static CELL: OnceLock<HashMap<String, String>> = OnceLock::new();
    CELL.get_or_init(load_dotenv_map)
}

/// Effective value of `key`: the process environment if present, otherwise the backend's
/// `.env`. Mirrors python-dotenv's `override=False` semantics used by `wavr.config`.
fn effective(key: &str) -> Option<String> {
    match std::env::var(key) {
        Ok(v) => Some(v),
        Err(_) => dotenv().get(key).cloned(),
    }
}

fn is_truthy(v: &str) -> bool {
    matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on")
}

fn is_falsy(v: &str) -> bool {
    matches!(v.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off")
}

fn multidevice() -> bool {
    effective("WAVR_MULTIDEVICE").map(|v| is_truthy(&v)).unwrap_or(false)
}

/// Default ON: notifications fire unless explicitly turned off.
fn notifications_enabled() -> bool {
    effective(NOTIFY_ENV).map(|v| !is_falsy(&v)).unwrap_or(true)
}

/// Default OFF: no login-item is created unless explicitly turned on.
fn autostart_enabled() -> bool {
    effective(AUTOSTART_ENV).map(|v| is_truthy(&v)).unwrap_or(false)
}

fn port() -> String {
    effective(PORT_ENV)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_PORT.to_string())
}

fn scheme() -> &'static str {
    if multidevice() {
        "https"
    } else {
        "http"
    }
}

fn backend_url() -> String {
    format!("{}://127.0.0.1:{}", scheme(), port())
}

// ---------------------------------------------------------------------------
// Local self-signed cert resolution + pinning (HTTPS mode only).
// Mirrors wavr.tls.resolved_cert_path / _default_dir so the shell pins EXACTLY the cert
// the backend serves: WAVR_TLS_CERT, else WAVR_TLS_DIR/cert.pem, else ~/.wavr/cert.pem.
// ---------------------------------------------------------------------------

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

fn resolved_cert_path() -> Option<PathBuf> {
    if let Some(c) = effective("WAVR_TLS_CERT").filter(|s| !s.trim().is_empty()) {
        return Some(PathBuf::from(c));
    }
    let dir = effective("WAVR_TLS_DIR")
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .or_else(|| home_dir().map(|h| h.join(".wavr")))?;
    Some(dir.join("cert.pem"))
}

/// DER bytes of the first CERTIFICATE block in a PEM buffer, or `None` if there is none.
fn first_cert_der_from_pem(pem: &[u8]) -> Option<Vec<u8>> {
    let mut reader = std::io::BufReader::new(pem);
    let first = rustls_pemfile::certs(&mut reader).next()?;
    let cert = first.ok()?;
    Some(cert.as_ref().to_vec())
}

/// DER of the live backend cert on disk, re-read each time so it tracks rotation.
fn pinned_cert_der() -> Option<Vec<u8>> {
    let path = resolved_cert_path()?;
    let pem = std::fs::read(path).ok()?;
    first_cert_der_from_pem(&pem)
}

/// A rustls verifier that trusts EXACTLY one certificate: the local backend's, by DER
/// byte-equality. The TLS signature checks are still delegated to the crypto provider, so
/// the peer must actually hold the pinned cert's private key. This is strict pinning, not
/// `danger_accept_invalid_certs` — a MitM's substituted cert is rejected.
struct PinnedServerCert {
    der: Vec<u8>,
    provider: Arc<rustls::crypto::CryptoProvider>,
}

impl std::fmt::Debug for PinnedServerCert {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PinnedServerCert").finish_non_exhaustive()
    }
}

impl ServerCertVerifier for PinnedServerCert {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        if end_entity.as_ref() == self.der.as_slice() {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(rustls::Error::General(
                "Wavr: server certificate does not match the pinned local cert".into(),
            ))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider.signature_verification_algorithms.supported_schemes()
    }
}

/// A ureq agent whose TLS trusts ONLY the pinned local cert.
fn pinned_https_agent(der: Vec<u8>) -> ureq::Agent {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let verifier = Arc::new(PinnedServerCert {
        der,
        provider: provider.clone(),
    });
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("rustls: default protocol versions")
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    ureq::builder()
        .timeout_connect(Duration::from_secs(5))
        .timeout_read(Duration::from_secs(5))
        .tls_config(Arc::new(config))
        .build()
}

// ---------------------------------------------------------------------------
// Backend supervision.
// ---------------------------------------------------------------------------

/// Resolve the Python interpreter: `WAVR_PYTHON`, else `python` on PATH. In dev, prefer
/// setting `WAVR_PYTHON` to the repo venv (…/.venv/Scripts/python.exe).
fn python() -> String {
    std::env::var("WAVR_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn spawn_backend() -> std::io::Result<Child> {
    let mut cmd = Command::new(python());
    cmd.args(["-m", "wavr.serve"]);
    // Run from the backend/repo dir if given, so the backend's load_dotenv() finds ./.env.
    // The shell deliberately does NOT set WAVR_MULTIDEVICE / WAVR_BIND / WAVR_TLS_*: the
    // backend owns that decision via its .env. We only pin the port so both sides agree.
    if let Ok(dir) = std::env::var("WAVR_BACKEND_DIR") {
        cmd.current_dir(dir);
    }
    cmd.env(PORT_ENV, port());
    // Windows: start suspended (the child's ONE thread exists but has not executed a
    // single instruction) so confine_backend_to_job_object() can assign the Job Object
    // BEFORE anything the child does -- closing the spawn -> assign TOCTOU race a fast
    // child could otherwise win. spawn_backend() never leaves it stuck suspended:
    // confine_backend_to_job_object() unconditionally resumes it (via
    // resume_suspended_process()) whether or not the Job Object steps themselves succeed.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(windows::Win32::System::Threading::CREATE_SUSPENDED.0);
    }
    cmd.spawn()
}

/// Block until the backend answers its readiness probe, or the timeout elapses. In HTTPS
/// mode this waits for the cert file to appear, then probes with the pinned agent.
fn wait_healthy(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let probe = format!("{}/api/state", backend_url());

    if scheme() == "https" {
        // The backend writes cert.pem before uvicorn binds, so once the port answers the
        // cert exists. Build the pinned agent lazily from the first successful cert read.
        let mut agent: Option<ureq::Agent> = None;
        while Instant::now() < deadline {
            if agent.is_none() {
                if let Some(der) = pinned_cert_der() {
                    agent = Some(pinned_https_agent(der));
                }
            }
            if let Some(a) = &agent {
                if a.get(&probe).call().is_ok() {
                    return true;
                }
            }
            std::thread::sleep(Duration::from_millis(400));
        }
        false
    } else {
        while Instant::now() < deadline {
            if ureq::get(&probe).timeout(Duration::from_secs(5)).call().is_ok() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(400));
        }
        false
    }
}

// ---------------------------------------------------------------------------
// Native OS notifications for high-severity backend alerts (intrusion / fall / rogue-DHCP /
// gateway-identity). Rust-side poller of GET /api/alerts only -- no JS-invokable
// `tauri::command` is added for this, so it adds no new capabilities/permissions surface
// for the webview (the dashboard never knows this is happening).
// ---------------------------------------------------------------------------

/// Mirrors `backend/wavr/alert_severity.py`'s ONE severity ladder (info < note < watch <
/// alert < critical). Only the ordering is needed here, to gate which alerts are "high
/// severity" enough to raise a native notification for -- kept as a small local constant
/// rather than a shared crate since Rust cannot import the Python module directly.
const SEVERITY_LADDER: [&str; 5] = ["info", "note", "watch", "alert", "critical"];

/// Rank of `severity` in the ladder (higher = more severe), or -1 for an unrecognized
/// value -- same honesty rule as the Python source: a malformed severity must never be
/// treated as urgent, nor crash this thread.
fn severity_rank(severity: &str) -> i32 {
    SEVERITY_LADDER
        .iter()
        .position(|&s| s == severity)
        .map(|i| i as i32)
        .unwrap_or(-1)
}

/// "alert" and "critical" only -- intrusion/fall_suspected/rogue_dhcp are always `alert`;
/// gateway_identity is `alert` on first detection, `critical` once sustained. Routine
/// rogue-device sightings (`info`/`note`) never reach this threshold, so ordinary device
/// churn on the LAN stays silent.
fn is_high_severity(severity: &str) -> bool {
    severity_rank(severity) >= severity_rank("alert")
}

/// Fallback text (ADR-0003) used if a `fall_suspected` alert is somehow missing its
/// `disclaimer` field -- keeps the notification honest even against a malformed payload.
const FALL_DISCLAIMER_FALLBACK: &str =
    "Research demonstration only -- not a medical device (ADR-0003).";

/// Short fields (room names, a rogue DHCP server's self-reported identifier) never
/// legitimately need more than this.
const SANITIZE_MAX_SHORT: usize = 120;
/// The ADR-0003 fall disclaimer (`backend/wavr/fall_detect.py::DISCLAIMER`) is a full
/// safety sentence, ~233 chars today -- `SANITIZE_MAX_SHORT` would truncate it mid-sentence
/// and silently drop its actual "never as a diagnosis" guidance. Generous enough for the
/// current text plus headroom for future wording changes, while still bounding a
/// pathological value.
const SANITIZE_MAX_LONG: usize = 500;

/// Defense-in-depth (item 7): the backend is a same-machine loopback process, not an
/// untrusted network peer, but `room` (operator map-editor text), `extra_server` (a rogue
/// DHCP server's self-reported option-54 identifier), and `disclaimer` all flow straight
/// from device-/backend-supplied strings into an OS notification with zero escaping today.
/// Strip control characters (which could otherwise inject stray lines into the
/// notification) and clamp length -- to `max_chars`, chosen per field by the caller so a
/// short field's clamp can't truncate the much longer disclaimer -- so one malformed or
/// oversized field can't corrupt the whole notification. Returns `None` for an
/// empty/all-control-chars result so callers can still fall back to their own generic
/// wording.
fn sanitize_field(raw: &str, max_chars: usize) -> Option<String> {
    let cleaned: String = raw.chars().filter(|c| !c.is_control()).collect();
    let trimmed = cleaned.trim();
    if trimmed.is_empty() {
        return None;
    }
    if trimmed.chars().count() > max_chars {
        let mut clamped: String = trimmed.chars().take(max_chars).collect();
        clamped.push('\u{2026}'); // "…"
        Some(clamped)
    } else {
        Some(trimmed.to_string())
    }
}

/// Human-readable (title, body) for one alert dict from GET /api/alerts. Every kind (see
/// `backend/wavr/api_inventory.py::merge_alerts`) carries `kind` + `severity`; the rest is
/// kind-specific. Read defensively (`unwrap_or` throughout) so a field shape we don't
/// recognize renders a blanker message instead of panicking this thread.
fn describe_alert(alert: &serde_json::Value) -> (String, String) {
    let kind = alert.get("kind").and_then(|v| v.as_str()).unwrap_or("alert");
    let room = alert
        .get("room")
        .and_then(|v| v.as_str())
        .and_then(|s| sanitize_field(s, SANITIZE_MAX_SHORT));
    let body = match kind {
        "intrusion" => match &room {
            Some(r) => format!("Unrecognized person detected in {r}."),
            None => "Unrecognized person detected.".to_string(),
        },
        "fall_suspected" => {
            // ADR-0003: the backend ships the non-diagnostic disclaimer in the SAME
            // payload (backend/wavr/fall_detect.py::FallAlert.to_dict) -- carry it
            // through to the notification instead of dropping it, so this alert never
            // reads like a medical/diagnostic claim on its own. SANITIZE_MAX_LONG (not
            // _SHORT): the real disclaimer text is ~233 chars, well past the short clamp.
            let disclaimer = alert
                .get("disclaimer")
                .and_then(|v| v.as_str())
                .and_then(|s| sanitize_field(s, SANITIZE_MAX_LONG))
                .unwrap_or_else(|| FALL_DISCLAIMER_FALLBACK.to_string());
            match &room {
                Some(r) => format!("Possible fall detected in {r}. {disclaimer}"),
                None => format!("Possible fall detected. {disclaimer}"),
            }
        }
        "rogue_dhcp" => {
            let server = alert
                .get("extra_server")
                .and_then(|v| v.as_str())
                .and_then(|s| sanitize_field(s, SANITIZE_MAX_SHORT))
                .unwrap_or_else(|| "an unknown server".to_string());
            format!("Rogue DHCP server detected on your network ({server}).")
        }
        "gateway_identity" => {
            if alert.get("severity").and_then(|v| v.as_str()) == Some("critical") {
                "Your network gateway's identity change has PERSISTED -- possible ARP/router spoofing."
                    .to_string()
            } else {
                "Your network gateway's identity changed unexpectedly.".to_string()
            }
        }
        other => format!("Wavr alert: {other}."),
    };
    ("Wavr Alert".to_string(), body)
}

/// One GET /api/alerts round-trip, using the same http/https + pinned-cert-agent split as
/// `wait_healthy()`. `https_agent` is cached across calls (lazily built once the cert is
/// readable) so we are not re-doing a TLS handshake setup on every poll tick.
fn fetch_alerts(url: &str, https_agent: &mut Option<ureq::Agent>) -> Option<Vec<serde_json::Value>> {
    let text = if scheme() == "https" {
        if https_agent.is_none() {
            *https_agent = pinned_cert_der().map(pinned_https_agent);
        }
        https_agent.as_ref()?.get(url).call().ok()?.into_string().ok()?
    } else {
        ureq::get(url).timeout(Duration::from_secs(5)).call().ok()?.into_string().ok()?
    };
    let parsed: serde_json::Value = serde_json::from_str(&text).ok()?;
    parsed.get("alerts")?.as_array().cloned()
}

/// Poll GET /api/alerts on a steady interval (never a busy loop) and raise a native OS
/// notification for each NEW high-severity alert. Must only be called once the backend is
/// already confirmed healthy (see the `wait_healthy()` call site in `setup()`).
///
/// Debounce: /api/alerts is an append-only, edge-triggered ring (each episode is appended
/// once, on the clear->flagged transition, and only re-arms after it clears) -- so tracking
/// the highest `ts` seen so far and only notifying for entries strictly newer than that
/// high-water mark naturally fires each episode exactly once, no matter the poll interval.
/// The very FIRST poll after launch only primes the high-water mark and notifies nothing --
/// otherwise every alert already sitting in the backend's ring from before this launch
/// would replay as "new" the moment the app starts. A burst is capped per tick (rolled up
/// into a "+N more" summary notification) so a long backend/notifier outage followed by a
/// reconnect can't flood the OS notification center.
fn spawn_alert_notifier(app: tauri::AppHandle) {
    if !notifications_enabled() {
        return;
    }
    std::thread::spawn(move || {
        const POLL_INTERVAL: Duration = Duration::from_secs(5);
        const MAX_PER_TICK: usize = 5;

        let url = format!("{}/api/alerts", backend_url());
        let mut https_agent: Option<ureq::Agent> = None;
        let mut high_water: Option<String> = None;

        loop {
            std::thread::sleep(POLL_INTERVAL);

            let Some(alerts) = fetch_alerts(&url, &mut https_agent) else {
                continue; // backend unreachable / not-yet-ready this tick -- retry next tick
            };
            if alerts.is_empty() {
                continue;
            }

            let max_ts = alerts
                .iter()
                .filter_map(|a| a.get("ts").and_then(|t| t.as_str()))
                .max()
                .map(str::to_string);

            let Some(hw) = high_water.clone() else {
                high_water = max_ts; // prime the baseline; never notify for pre-launch history
                continue;
            };

            let mut new_alerts: Vec<&serde_json::Value> = alerts
                .iter()
                .filter(|a| {
                    a.get("ts")
                        .and_then(|t| t.as_str())
                        .map(|t| t > hw.as_str())
                        .unwrap_or(false)
                })
                .filter(|a| {
                    a.get("severity")
                        .and_then(|s| s.as_str())
                        .map(is_high_severity)
                        .unwrap_or(false)
                })
                .collect();
            new_alerts.sort_by(|a, b| {
                let ta = a.get("ts").and_then(|t| t.as_str()).unwrap_or("");
                let tb = b.get("ts").and_then(|t| t.as_str()).unwrap_or("");
                ta.cmp(tb)
            });

            if let Some(new_max) = max_ts {
                high_water = Some(new_max);
            }

            if new_alerts.is_empty() {
                continue;
            }

            let overflow = new_alerts.len().saturating_sub(MAX_PER_TICK);
            for alert in new_alerts.iter().take(MAX_PER_TICK) {
                let (title, body) = describe_alert(alert);
                let _ = app.notification().builder().title(title).body(body).show();
            }
            if overflow > 0 {
                let _ = app
                    .notification()
                    .builder()
                    .title("Wavr")
                    .body(format!("+{overflow} more alert(s) — open Wavr for details"))
                    .show();
            }
        }
    });
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Some(mut child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

// ---------------------------------------------------------------------------
// Local log file: release builds set `windows_subsystem = "windows"` (no console), so a
// bare `eprintln!` from a background-setup failure path is silently lost in the field --
// there is no console to attach and read it from. `log_issue()` mirrors it to a small
// local log file too, so a crash-safety claim that's easy to verify with a console
// (`cargo tauri dev`) stays verifiable after the fact in a release install as well.
// ---------------------------------------------------------------------------

/// Append one line to `~/.wavr/desktop.log` (best-effort: a logging failure must never
/// itself crash or block startup). `eprintln!` first, so a console-attached run (dev
/// builds, or a release build launched from a terminal) still sees it immediately.
fn log_issue(msg: &str) {
    eprintln!("{msg}");
    let Some(home) = home_dir() else { return };
    let dir = home.join(".wavr");
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("desktop.log"))
    {
        // Unix-epoch seconds (UTC), not a formatted timestamp -- avoids pulling in a
        // date/time-formatting dependency just for a log line; any support engineer can
        // convert it (`date -d @<secs>` or equivalent).
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(f, "[{now}] {msg}");
    }
}

/// Resume every thread owned by `pid` -- expected to be exactly one: the primary thread a
/// `CREATE_SUSPENDED` spawn leaves suspended before it has executed a single instruction.
/// `std::process::Child` does not expose the thread `HANDLE` `CreateProcessW` itself
/// returns, so this is the standard workaround: enumerate the process's threads via a
/// toolhelp snapshot and `ResumeThread` each one found. Always called by
/// `confine_backend_to_job_object()` regardless of whether the Job Object steps
/// themselves succeeded -- the suspension exists solely to close the TOCTOU race, never to
/// gate the backend on Job Object support.
#[cfg(windows)]
fn resume_suspended_process(pid: u32) {
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    use windows::Win32::System::Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME};

    unsafe {
        let snapshot = match CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) {
            Ok(h) => h,
            Err(e) => {
                log_issue(&format!(
                    "Wavr: CreateToolhelp32Snapshot failed while resuming the suspended backend (pid {pid} may be stuck suspended): {e}"
                ));
                return;
            }
        };

        let mut entry = THREADENTRY32 {
            dwSize: std::mem::size_of::<THREADENTRY32>() as u32,
            ..Default::default()
        };
        let mut resumed = 0u32;
        if Thread32First(snapshot, &mut entry).is_ok() {
            loop {
                if entry.th32OwnerProcessID == pid {
                    if let Ok(th) = OpenThread(THREAD_SUSPEND_RESUME, false, entry.th32ThreadID) {
                        // ResumeThread returns the thread's PREVIOUS suspend count, or
                        // (DWORD) -1 (u32::MAX) on failure -- only count a thread as
                        // actually resumed when it did not fail, so `resumed == 0` below
                        // can't be silently wrong about whether the child is still stuck.
                        let prev_suspend_count = ResumeThread(th);
                        let _ = CloseHandle(th);
                        if prev_suspend_count != u32::MAX {
                            resumed += 1;
                        } else {
                            log_issue(&format!(
                                "Wavr: ResumeThread failed for thread {} of pid {pid}",
                                entry.th32ThreadID
                            ));
                        }
                    }
                }
                if Thread32Next(snapshot, &mut entry).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snapshot);

        if resumed == 0 {
            log_issue(&format!(
                "Wavr: found no thread of pid {pid} to resume -- the backend is likely stuck suspended and will never answer its readiness probe"
            ));
        }
    }
}

/// Confine the spawned backend to a Windows Job Object with KILL_ON_JOB_CLOSE, so the OS
/// force-kills it on ANY exit path of this process — crash, panic, `taskkill`/Task Manager
/// on wavr-desktop.exe, power-loss handler — not only our own graceful `kill_backend()`
/// (tray-Quit / `RunEvent::ExitRequested`). This is the crash-safety net that closes the
/// "orphaned sidecar holds port 8000" gap: `child.kill()` alone only runs if our code gets
/// to run at all.
///
/// The returned job `HANDLE` is deliberately never closed (no `CloseHandle`/`.free()`): it
/// must outlive this process. Windows closes every handle a process still holds when that
/// process terminates by any means, and closing the *last* handle to a KILL_ON_JOB_CLOSE job
/// is exactly what terminates every process still assigned to it — so "leaking" this handle
/// for the process's natural lifetime is the mechanism, not an oversight.
///
/// `spawn_backend()` starts the child `CREATE_SUSPENDED` (Windows only) specifically so
/// this function can assign it to the Job Object BEFORE it has run a single instruction --
/// otherwise a pathologically fast child could exit (or reparent/spawn its own children)
/// before this function ever gets to run, escaping the job. Whatever happens above,
/// `resume_suspended_process()` always runs last so the child is never left stuck
/// suspended just because a Job Object step failed.
#[cfg(windows)]
fn confine_backend_to_job_object(child: &Child) {
    use std::os::windows::io::AsRawHandle;
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    let pid = child.id();

    // A closure (not early `return`s directly in the function body) so there is exactly
    // ONE call to `resume_suspended_process()` below, on every path -- no risk of a future
    // edit adding a new failure branch that forgets to resume the child.
    let outcome: Result<(), String> = (|| unsafe {
        let job = CreateJobObjectW(None, PCWSTR::null())
            .map_err(|e| format!("Wavr: CreateJobObjectW failed (no crash-safety net for the sidecar): {e}"))?;

        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
        .map_err(|e| format!("Wavr: SetInformationJobObject failed (no crash-safety net for the sidecar): {e}"))?;

        let process = HANDLE(child.as_raw_handle());
        AssignProcessToJobObject(job, process)
            .map_err(|e| format!("Wavr: AssignProcessToJobObject failed (no crash-safety net for the sidecar): {e}"))
        // `job` is intentionally dropped here without `CloseHandle` -- see the doc comment
        // above: it must outlive this process for KILL_ON_JOB_CLOSE to do its job.
    })();

    if let Err(msg) = outcome {
        log_issue(&msg);
    }

    // Runs whether the Job Object steps above succeeded or failed: the suspension's only
    // purpose is closing the TOCTOU race, so the child must never be left stuck suspended
    // just because Job Object support itself failed.
    resume_suspended_process(pid);
}

// ---------------------------------------------------------------------------
// Windows: scoped WebView2 cert pinning for the loopback-HTTPS backend.
// ---------------------------------------------------------------------------

/// Authority (`host:port`) of a URI, e.g. `https://127.0.0.1:8000/x` -> `127.0.0.1:8000`.
#[cfg(windows)]
fn authority_of(uri: &str) -> &str {
    let after = uri.splitn(2, "://").nth(1).unwrap_or(uri);
    let end = after
        .find(['/', '?', '#'])
        .unwrap_or(after.len());
    &after[..end]
}

/// Register a ServerCertificateErrorDetected handler that AlwaysAllow-s ONLY the exact
/// pinned loopback cert on 127.0.0.1:<port>, and CANCELs everything else. Must run before
/// the window ever navigates to the HTTPS backend.
#[cfg(windows)]
fn install_cert_pinning(app: &tauri::AppHandle) {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        ICoreWebView2_14, COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_ALWAYS_ALLOW,
        COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_CANCEL,
    };
    use webview2_com::{take_pwstr, ServerCertificateErrorDetectedEventHandler};
    use windows::core::{Interface, PWSTR};

    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let expected_authority = format!("127.0.0.1:{}", port());

    let registered = window.with_webview(move |webview| unsafe {
        let core = match webview.controller().CoreWebView2() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("Wavr: could not get CoreWebView2 for cert pinning: {e}");
                return;
            }
        };
        let core14: ICoreWebView2_14 = match core.cast() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("Wavr: WebView2 runtime too old for cert pinning (need ICoreWebView2_14): {e}");
                return;
            }
        };

        let handler = ServerCertificateErrorDetectedEventHandler::create(Box::new(
            move |_sender, args| {
                let Some(args) = args else {
                    return Ok(());
                };

                // Scope 1: request authority must be exactly our loopback backend.
                let mut uri_ptr = PWSTR::null();
                let _ = args.RequestUri(&mut uri_ptr);
                let uri = take_pwstr(uri_ptr);
                let mut allow = false;
                if authority_of(&uri) == expected_authority {
                    // Scope 2: presented cert must be byte-identical to the pinned cert.
                    if let Ok(cert) = args.ServerCertificate() {
                        let mut pem_ptr = PWSTR::null();
                        if cert.ToPemEncoding(&mut pem_ptr).is_ok() {
                            let pem = take_pwstr(pem_ptr);
                            if let (Some(presented), Some(pinned)) =
                                (first_cert_der_from_pem(pem.as_bytes()), pinned_cert_der())
                            {
                                allow = presented == pinned;
                            }
                        }
                    }
                }

                let action = if allow {
                    COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_ALWAYS_ALLOW
                } else {
                    COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_CANCEL
                };
                let _ = args.SetAction(action);
                Ok(())
            },
        ));

        let mut token = 0i64;
        if let Err(e) = core14.add_ServerCertificateErrorDetected(&handler, &mut token) {
            eprintln!("Wavr: failed to install WebView2 cert pin: {e}");
        }
    });

    if let Err(e) = registered {
        eprintln!("Wavr: with_webview failed while installing cert pin: {e}");
    }
}

// ---------------------------------------------------------------------------
// Item 1 (CRITICAL perf): suspend/resume the WebView2 render loop on hide/show.
//
// The dashboard's three.js scene and ambient wave (frontend/index.html) both gate their
// render loops on `document.hidden`/`visibilitychange` -- that's how a real browser tab
// stops burning CPU when you switch away. But Tauri's `window.hide()`/`show()` only toggle
// the top-level HWND (`tauri-runtime-wry`'s `WindowMessage::Hide/Show` -> the tao window's
// own `set_visible()`); WebView2 has a SEPARATE `IsVisible` concept on
// `ICoreWebView2Controller` that Tauri never touches, so `document.hidden` never flips and
// `visibilitychange` never fires from a Tauri hide/show alone. Left unaddressed, a
// tray-hidden or autostart-start-hidden window keeps compositing full 3D frames forever --
// a full CPU core burned for a window nobody can even see, defeating the always-on
// Desktop-as-Core purpose (ADR-0007).
//
// `ICoreWebView2_3::TrySuspend` pauses WebView2's own script timers/rAF and shrinks the
// renderer process; per Microsoft's docs it requires the controller's `IsVisible` to
// already read `false` (fails with ERROR_INVALID_STATE otherwise) -- Tauri never sets that
// for us (see above), so `set_webview_suspended()` sets it explicitly, in the same order
// MS's own guidance describes ("useful when a Win32 app becomes invisible"). `Resume()` is
// the inverse, called BEFORE `SetIsVisible(true)` so the page is already live again by the
// time the controller (and OS window) become visible. Both directions are best-effort: a
// failure here only costs the CPU saving, never blocks hide/show/startup -- same
// `log_issue()`-and-move-on posture as every other WebView2 quirk in this file (cert
// pinning, Job Objects).
// ---------------------------------------------------------------------------

#[cfg(windows)]
fn set_webview_suspended(app: &tauri::AppHandle, suspend: bool) {
    use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2_3;
    use webview2_com::TrySuspendCompletedHandler;
    use windows::core::Interface;

    let Some(window) = app.get_webview_window("main") else {
        return;
    };

    let registered = window.with_webview(move |webview| unsafe {
        let controller = webview.controller();
        let core = match controller.CoreWebView2() {
            Ok(c) => c,
            Err(e) => {
                log_issue(&format!(
                    "Wavr: could not get CoreWebView2 to {} the render loop: {e}",
                    if suspend { "suspend" } else { "resume" }
                ));
                return;
            }
        };
        let core3: ICoreWebView2_3 = match core.cast() {
            Ok(c) => c,
            Err(e) => {
                log_issue(&format!(
                    "Wavr: WebView2 runtime too old to {} the render loop (need ICoreWebView2_3): {e}",
                    if suspend { "suspend" } else { "resume" }
                ));
                return;
            }
        };

        if suspend {
            // TrySuspend requires the controller to already report itself invisible --
            // see the module doc comment above for why Tauri's own hide() never does
            // this for us.
            if let Err(e) = controller.SetIsVisible(false) {
                log_issue(&format!("Wavr: SetIsVisible(false) before TrySuspend failed: {e}"));
            }
            let handler = TrySuspendCompletedHandler::create(Box::new(move |result, succeeded| {
                if let Err(e) = result {
                    log_issue(&format!("Wavr: TrySuspend completed with an error: {e}"));
                } else if !succeeded {
                    log_issue(
                        "Wavr: TrySuspend did not report success -- the hidden window's render loop may still be running",
                    );
                }
                Ok(())
            }));
            if let Err(e) = core3.TrySuspend(&handler) {
                log_issue(&format!("Wavr: TrySuspend call failed: {e}"));
            }
        } else {
            // Resume works even while the controller still reads invisible -- do it
            // first so content is already fresh by the time SetIsVisible(true) (and the
            // OS window show) actually happen.
            if let Err(e) = core3.Resume() {
                log_issue(&format!("Wavr: Resume failed: {e}"));
            }
            if let Err(e) = controller.SetIsVisible(true) {
                log_issue(&format!("Wavr: SetIsVisible(true) after Resume failed: {e}"));
            }
        }
    });

    if let Err(e) = registered {
        log_issue(&format!("Wavr: with_webview failed while toggling suspend state: {e}"));
    }
}

/// macOS/Linux: `ICoreWebView2_3::TrySuspend` is a WebView2 (Windows-only) API. The window
/// still hides/shows normally via `hide_window()`/`show_window()` below -- this is only the
/// extra render-loop-suspend step, which simply has no equivalent wired up on those
/// platforms yet.
#[cfg(not(windows))]
fn set_webview_suspended(_app: &tauri::AppHandle, _suspend: bool) {}

/// Hide the main window (close-to-tray / tray-driven hide) AND, on Windows, suspend the
/// WebView2 render loop so a hidden Wavr Desktop stops burning a CPU core. ALWAYS use this
/// (never a bare `window.hide()`) so every hide path gets the CPU fix.
fn hide_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.hide();
    }
    set_webview_suspended(app, true);
}

/// Show the main window (tray "Open Wavr" / second-instance relaunch / initial
/// non-autostart launch) AND, on Windows, resume the WebView2 render loop FIRST so the
/// dashboard is already live the instant the window becomes visible. ALWAYS use this
/// (never a bare `window.show()`) so every show path un-suspends the render loop.
fn show_window(app: &tauri::AppHandle) {
    set_webview_suspended(app, false);
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn main() {
    // Set once, before the builder, and moved into setup(): whether THIS launch was
    // started by the OS autostart mechanism (see the `tauri_plugin_autostart::init()` arg
    // below, which appends AUTOSTART_ARG only to the login-item command line -- a manual
    // launch, or `cargo tauri dev`, never has it). This is the sole signal for "start
    // hidden to tray" (item 5) — everything else about startup is unchanged.
    let autostart_launch = std::env::args().any(|a| a == AUTOSTART_ARG);

    tauri::Builder::default()
        // Must be the first plugin registered (tauri-plugin-single-instance's own
        // requirement): a second launch hits this callback in the ALREADY-RUNNING instance
        // instead of continuing its own startup, so it never spawns a second
        // `python -m wavr.serve` to race the first for the same port — that race is exactly
        // what left second launches silently stuck on the "Starting…" placeholder before
        // this guard existed. Desktop-as-Core (running this shell headless/on-login as a
        // durable always-on peer, mirroring the G9 Core) depends on this: without it, a
        // second accidental launch would corrupt the single running backend's port instead
        // of being a no-op.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_window(app);
        }))
        // Item 5: launch-on-login (opt-in, default OFF -- see autostart_enabled()). The
        // AUTOSTART_ARG marker is what setup() below checks to decide whether THIS launch
        // should start hidden. Rust-only usage (app.autolaunch()...), no JS-invokable
        // command wired up, so no capabilities/permissions entry is needed for the webview
        // even though the plugin's own commands exist (enable/disable/is_enabled) --
        // they're simply never exposed.
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec![AUTOSTART_ARG]),
        ))
        // Item 4: native OS alerts. Rust-only usage (app.notification()...) from
        // spawn_alert_notifier() below -- same "no JS-invokable command, no capabilities
        // entry" reasoning as autostart above.
        .plugin(tauri_plugin_notification::init())
        .manage(Backend(Mutex::new(None)))
        .setup(move |app| {
            // 1. spawn the backend and remember it for cleanup.
            match spawn_backend() {
                Ok(child) => {
                    // Crash-safety net: force-kill on ANY process exit, not just our own
                    // graceful kill_backend() paths. See confine_backend_to_job_object().
                    #[cfg(windows)]
                    confine_backend_to_job_object(&child);
                    *app.state::<Backend>().0.lock().unwrap() = Some(child);
                }
                Err(e) => eprintln!("failed to start Wavr backend: {e}"),
            }

            // 2. window visibility. The window is declared `"visible": false` in
            //    tauri.conf.json so EVERY launch creates it hidden with zero flash; a
            //    normal (manual / dev / tray-relaunch-via-second-instance) launch shows it
            //    immediately here, exactly matching the pre-existing "Starting…"-then-
            //    navigate UX. An autostart-triggered launch stays hidden -- only tray
            //    "Open Wavr" (or a second manual launch, via the single-instance handler
            //    above) reveals it -- so the backend still comes up and starts sensing
            //    immediately, just without a window ever appearing.
            if !autostart_launch {
                show_window(app.handle());
            }

            // 3. reconcile the login-item state from WAVR_DESKTOP_AUTOSTART every launch
            //    (declarative, same pattern as WAVR_MULTIDEVICE: the env var is the one
            //    source of truth, no separate persisted on/off flag). Default OFF.
            let autolaunch = app.autolaunch();
            let want_autostart = autostart_enabled();
            match autolaunch.is_enabled() {
                Ok(is_on) if is_on != want_autostart => {
                    let result = if want_autostart {
                        autolaunch.enable()
                    } else {
                        autolaunch.disable()
                    };
                    if let Err(e) = result {
                        let action = if want_autostart { "enable" } else { "disable" };
                        log_issue(&format!("Wavr: failed to {action} launch-on-login: {e}"));
                    }
                }
                Ok(_) => {} // already in the wanted state
                Err(e) => log_issue(&format!("Wavr: could not read launch-on-login state: {e}")),
            }

            // 4. tray icon + menu.
            let open = MenuItemBuilder::with_id("open", "Open Wavr").build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
            let menu = MenuBuilder::new(app).items(&[&open, &quit]).build()?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Wavr Desktop")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => show_window(app),
                    "quit" => {
                        kill_backend(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            // 5. HTTPS mode: install the scoped cert pin BEFORE any navigation happens.
            #[cfg(windows)]
            if multidevice() {
                install_cert_pinning(app.handle());
            }

            // 6. once the backend is healthy, navigate the window to it (done off-thread so
            //    setup() returns immediately and the "Starting…" placeholder shows
            //    meanwhile, when the window is shown at all) and start the native-alert
            //    poller (item 4). Item 3: if the backend NEVER becomes healthy (wrong
            //    WAVR_PYTHON, a missing dep, a zombie process already holding the port --
            //    all seen in this repo's history), surface that instead of leaving the
            //    window stuck on "Starting Wavr…" forever with no recourse.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let url = backend_url();
                if wait_healthy(HEALTH_TIMEOUT) {
                    if let Some(w) = handle.get_webview_window("main") {
                        if let Ok(u) = url.parse::<tauri::Url>() {
                            let _ = w.navigate(u);
                        }
                    }
                    spawn_alert_notifier(handle.clone());
                    if autostart_launch {
                        // This launch's window started (and stays) hidden -- nothing
                        // ever calls hide_window() for it, so nothing would otherwise
                        // trigger the item-1 suspend below. Do it here so a headless
                        // Desktop-as-Core launch doesn't run the now-live dashboard's
                        // render loop unthrottled forever just because it was never
                        // shown in the first place.
                        //
                        // BUT re-check CURRENT visibility first: the tray "Open Wavr"
                        // item (built in step 4 above) is already live during this up-
                        // to-HEALTH_TIMEOUT wait, so the user may have clicked it and
                        // show_window()'d (resumed + shown) the placeholder before the
                        // backend became healthy. `autostart_launch` is a static,
                        // start-of-process flag -- it can't see that. Suspending
                        // unconditionally here would re-freeze that now-visible,
                        // user-opened window right after navigate(). Only suspend if
                        // the window is STILL hidden (autolaunch()'d and never opened).
                        let still_hidden = handle
                            .get_webview_window("main")
                            .and_then(|w| w.is_visible().ok())
                            .map(|visible| !visible)
                            .unwrap_or(false);
                        if still_hidden {
                            set_webview_suspended(&handle, true);
                        }
                    }
                } else {
                    let msg = format!(
                        "Wavr backend did not answer at {url} within {}s. Check WAVR_PYTHON, \
                         that the backend's dependencies are installed, and that no leftover \
                         process is already holding the port -- see ~/.wavr/desktop.log for \
                         details.",
                        HEALTH_TIMEOUT.as_secs()
                    );
                    log_issue(&format!("Wavr: {msg}"));
                    // Replace the "Starting Wavr…" placeholder's own copy in place (the
                    // window never navigated anywhere -- the backend isn't up) so opening
                    // it (now or via tray "Open Wavr" later) shows the real reason instead
                    // of an eternal spinner. `WebviewWindow::eval()` is Tauri's own
                    // sanctioned API for running JS inside the embedded webview (not the JS
                    // `eval()` anti-pattern on untrusted input): `msg` is Rust-authored
                    // (this process's own diagnostic text, no external/network/user input),
                    // and `serde_json::to_string` renders it as a properly quoted/escaped JS
                    // string literal before it's spliced into the script, so it cannot break
                    // out of the string or inject extra statements.
                    if let Some(w) = handle.get_webview_window("main") {
                        if let Ok(js_msg) = serde_json::to_string(&msg) {
                            let _ = w.eval(format!(
                                "window.wavrShowStartupError && window.wavrShowStartupError({js_msg});"
                            ));
                        }
                    }
                    if notifications_enabled() {
                        let _ = handle
                            .notification()
                            .builder()
                            .title("Wavr did not start")
                            .body(
                                "The backend never became healthy. Open Wavr for details, or \
                                 check ~/.wavr/desktop.log.",
                            )
                            .show();
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window hides to tray; the central keeps sensing.
            if let WindowEvent::CloseRequested { api, .. } = event {
                hide_window(window.app_handle());
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
