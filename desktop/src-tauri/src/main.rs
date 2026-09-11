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
//   3. poll   <scheme>://127.0.0.1:<port>/healthz  until it answers, then navigate the
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
use tauri::menu::CheckMenuItemBuilder;
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_notification::NotificationExt;

const PORT_ENV: &str = "WAVR_PORT";
const DEFAULT_PORT: &str = "8000";

/// How long `setup()`'s background thread waits for the backend's readiness probe before
/// giving up (item 3). Named so the timeout UX's own message can quote the same number
/// `wait_healthy()` is actually called with.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(45);

/// How long the shell keeps quietly probing AFTER the wait above has reported
/// failure and put a message on screen.
///
/// This exists because the first answer was being treated as the last one. On
/// the first user's machine the Core answered at 27 seconds — three past a
/// 20-second wait — and then served for a minute and a half while the window
/// said "Wavr didn't start" and nothing in the shell was still looking.
///
/// 45 seconds above is what a cold start of a 40MB self-extracting binary
/// actually costs on a laptop that is scanning it for the first time; this is
/// the margin for the machine that is slower than the one it was measured on.
/// Neither number is a promise, which is the point of having both.
const LATE_START_GRACE: Duration = Duration::from_secs(120);

/// Opt-out (default ON) for the native-OS-notification poller (item 4): set to
/// 0/false/no/off to silence it. Unlike every other `WAVR_*` var here this is a shell-only
/// setting -- the backend has no notion of it.
const NOTIFY_ENV: &str = "WAVR_DESKTOP_NOTIFICATIONS";
/// Opt-in (default OFF) launch-on-login (item 5). The env var supplies the DEFAULT;
/// once somebody toggles it in the tray that choice is recorded in
/// `~/.wavr/autostart` and wins on every later launch. See `autostart_enabled`.
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
/// Whether Wavr should start with the machine.
///
/// The env var is the DEFAULT, not the truth. A person who toggles this in the
/// tray has made a choice, and reconciling that away on the next launch — which
/// is what a purely declarative env var does — is a UI that silently undoes the
/// user. So a choice, once made, is recorded and wins.
///
/// The file holds "1" or "0" and nothing else. It exists only when somebody has
/// actually decided; its absence means "nobody has said, use the default",
/// which is a different fact from "somebody said no".
fn autostart_enabled() -> bool {
    if let Some(chosen) = autostart_choice() {
        return chosen;
    }
    effective(AUTOSTART_ENV).map(|v| is_truthy(&v)).unwrap_or(false)
}

fn autostart_choice_path() -> Option<std::path::PathBuf> {
    home_dir().map(|h| h.join(".wavr").join("autostart"))
}

fn autostart_choice() -> Option<bool> {
    let text = std::fs::read_to_string(autostart_choice_path()?).ok()?;
    match text.trim() {
        "1" => Some(true),
        "0" => Some(false),
        _ => None,
    }
}

/// Record the person's choice, and apply it now.
///
/// Returns what the state actually IS afterwards, read back from the OS rather
/// than assumed from what was asked for: a login item that failed to register
/// and a tick that says it did is precisely the kind of quiet lie this whole
/// area of the product is being rebuilt to remove.
fn set_autostart(app: &tauri::AppHandle, on: bool) -> bool {
    if let Some(path) = autostart_choice_path() {
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if let Err(e) = std::fs::write(&path, if on { "1" } else { "0" }) {
            log_issue(&format!("Wavr: could not record the autostart choice: {e}"));
        }
    }
    let launcher = app.autolaunch();
    let result = if on { launcher.enable() } else { launcher.disable() };
    if let Err(e) = result {
        log_issue(&format!("Wavr: could not change launch-on-login: {e}"));
    }
    launcher.is_enabled().unwrap_or(on)
}

fn port() -> String {
    effective(PORT_ENV)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_PORT.to_string())
}

/// The Job Object holding the current backend, as a raw handle value.
///
/// Every spawn creates one (see `confine_backend_to_job_object`) and deliberately
/// leaks it so KILL_ON_JOB_CLOSE outlives this function. Keeping the value here
/// costs nothing and buys the one thing the leak took away: the ability to
/// terminate the whole tree on purpose, rather than only when the shell exits.
///
/// A raw `usize` rather than a `HANDLE` because a pointer is not `Send`, and this
/// is read from the tray thread.
#[cfg(windows)]
static BACKEND_JOB: std::sync::Mutex<Option<usize>> = std::sync::Mutex::new(None);

/// The scheme the Core is ACTUALLY serving, as of the last time one answered.
///
/// Re-learned, not learned once. This was a `OnceLock`, which is wrong for the
/// same reason predicting the scheme was wrong: it is a property of the RUNNING
/// Core, and the Core is replaced during a normal session -- by the tray's
/// "Restart Core" and by the crash watchdog. Turn "Let other devices connect"
/// on, restart the Core, and the probe finds https, says so in the log, and
/// then sends the window to the http it had already committed to. Every request
/// on that page fails, the service worker answers the navigation from its
/// offline cache, and a fully drawn dashboard appears showing the sample house.
static LIVE_SCHEME: std::sync::Mutex<Option<&'static str>> = std::sync::Mutex::new(None);

/// Record what just answered. Called by `wait_healthy` on every success, so a
/// Core that comes back on the other scheme is followed rather than argued with.
fn remember_scheme(s: &'static str) {
    if let Ok(mut g) = LIVE_SCHEME.lock() {
        *g = Some(s);
    }
}

/// First guess, from the environment. Right in the common case, and free.
fn guessed_scheme() -> &'static str {
    if multidevice() {
        "https"
    } else {
        "http"
    }
}

/// What the shell should talk to the Core over.
///
/// Discovered, not predicted. `multidevice()` reads the process environment and
/// the backend's `.env`; the Settings screen writes the same switch into the
/// Core's DATABASE, which this shell cannot see and should not learn to read --
/// that would be a third copy of one decision. So the environment is only the
/// opening guess, and `wait_healthy` replaces it with whatever actually
/// answered.
///
/// Before that: the guess. After: the truth. The failure this removes was
/// visible to the first person who ever flipped that switch in the UI -- the
/// Core came up on HTTPS, the shell asked HTTP, and the window said "Wavr
/// didn't start" about a Core that was serving perfectly.
fn scheme() -> &'static str {
    LIVE_SCHEME
        .lock()
        .ok()
        .and_then(|g| *g)
        .unwrap_or_else(guessed_scheme)
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

/// The bundled Core executable, when this is an installed build.
///
/// ADR-0007 chose "spawn-not-bundle" and named a self-contained sidecar as the
/// follow-up. This is it. Without one, installing Wavr starts with "install
/// Python 3.11 or newer and tick Add to PATH" -- precisely the developer
/// knowledge the install experience is meant to remove.
///
/// Looked for next to our own executable, which is where Tauri's bundler places
/// `externalBin` output. Returns `None` from a `cargo run`/dev tree, so the
/// developer path below is untouched.
fn bundled_core() -> Option<std::path::PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let name = if cfg!(windows) { "wavr-core.exe" } else { "wavr-core" };
    for candidate in [dir.join(name), dir.join("wavr-core").join(name)] {
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

fn spawn_backend() -> std::io::Result<Child> {
    // Resolution order, and why: an explicit WAVR_PYTHON always wins (a
    // developer pointing at a venv); then the bundled sidecar (the installed
    // case, no Python on the machine); then `python -m wavr.serve` (a checkout,
    // exactly as before this existed).
    let explicit_python = std::env::var("WAVR_PYTHON").is_ok();
    let mut cmd = match bundled_core() {
        Some(core) if !explicit_python => {
            log_issue(&format!("spawning bundled Core: {}", core.display()));
            Command::new(core)
        }
        _ => {
            let mut c = Command::new(python());
            c.args(["-m", "wavr.serve"]);
            c
        }
    };
    // Run from the backend/repo dir if given, so the backend's load_dotenv() finds ./.env.
    // The shell deliberately does NOT set WAVR_MULTIDEVICE / WAVR_BIND / WAVR_TLS_*: the
    // backend owns that decision via its .env. We only pin the port so both sides agree.
    if let Ok(dir) = std::env::var("WAVR_BACKEND_DIR") {
        cmd.current_dir(dir);
    }
    cmd.env(PORT_ENV, port());
    // A STABLE home for the data, for the bundled Core only.
    //
    // The backend resolves `WAVR_DB` and `WAVR_HOUSE_MAP` relative to the
    // process's working directory when they are unset, and this shell sets no
    // working directory for the bundled case -- so the database landed wherever
    // the shell happened to be launched from. Measured on the installed app:
    // launched from the Start Menu it wrote into the install directory, and
    // launched from elsewhere it wrote a second, separate 274 KB database into
    // the user's profile. Two databases, two Spaces, and opening Wavr a
    // different way one morning drops the operator into the first-run wizard
    // with their whole home apparently gone -- no error, nothing said, and the
    // real data sitting in a file they have no reason to know about. A pinned
    // taskbar shortcut is enough to change the working directory.
    //
    // `~/.wavr` is where the local certificate already lives, so this adds no
    // new location to know about. An existing value always wins: a developer
    // with their own `.env`, and the `python -m wavr.serve` checkout path, are
    // both byte-identical to before.
    //
    // Consequence, deliberate and worth stating: with a stable home,
    // uninstalling no longer erases the Space. Starting over becomes the
    // explicit act the product already offers rather than a side effect of
    // removing the app.
    if bundled_core().is_some() && std::env::var("WAVR_BACKEND_DIR").is_err() {
        if let Some(home) = home_dir() {
            let data = home.join(".wavr");
            if std::fs::create_dir_all(&data).is_ok() {
                if std::env::var("WAVR_DB").is_err() {
                    cmd.env("WAVR_DB", data.join("wavr.db"));
                }
                if std::env::var("WAVR_HOUSE_MAP").is_err() {
                    cmd.env("WAVR_HOUSE_MAP", data.join("house.json"));
                }
            } else {
                // Could not create it -- say so rather than silently falling
                // back to the wandering working directory.
                log_issue(&format!(
                    "could not create the data directory {}; the Core will use \
                     its working directory, which moves with how Wavr is launched",
                    data.display()
                ));
            }
        }
    }
    // Windows: start suspended (the child's ONE thread exists but has not executed a
    // single instruction) so confine_backend_to_job_object() can assign the Job Object
    // BEFORE anything the child does -- closing the spawn -> assign TOCTOU race a fast
    // child could otherwise win. spawn_backend() never leaves it stuck suspended:
    // confine_backend_to_job_object() unconditionally resumes it (via
    // resume_suspended_process()) whether or not the Job Object steps themselves succeed.
    // CREATE_NO_WINDOW, alongside it, because the frozen Core is a CONSOLE
    // binary and this shell is a GUI one. Nothing here redirects its stdio, so
    // Windows gave the child a console of its own: a black window that sat
    // behind the dashboard for as long as Wavr was running. The first person to
    // install it said so in the first minute -- "esse prompt aberto no fundo eh
    // feio, eu queria que o wavr fosse so o programa mesmo" -- and he is right;
    // nothing reads that output, so the window was pure cost.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows::Win32::System::Threading::{CREATE_NO_WINDOW, CREATE_SUSPENDED};
        cmd.creation_flags(CREATE_SUSPENDED.0 | CREATE_NO_WINDOW.0);
    }

    // Confined and resumed HERE, not by the caller.
    //
    // It used to be the caller's job, and there are three callers: first launch,
    // the crash watchdog, and the tray's "Restart Core". Two of them remembered.
    // The third spawned a child `CREATE_SUSPENDED` and returned it to a function
    // that stored the handle, logged "Core restarted from the tray", and left
    // the process suspended forever -- one thread, `WaitReason=Suspended`, zero
    // CPU time consumed, no port, no PyInstaller extraction directory, nothing.
    // The first user restarted the Core and watched the dashboard sit on
    // "reconnecting..." against a Core that had never executed an instruction.
    //
    // Suspending the child is not optional -- it closes the spawn->assign race
    // that lets a fast child escape the Job Object -- so the resume cannot be
    // optional either, and the only way to guarantee that is to leave no caller
    // with the opportunity to forget. A `Child` handed out by this function has
    // already been confined and resumed.
    let child = cmd.spawn()?;
    #[cfg(windows)]
    confine_backend_to_job_object(&child);
    Ok(child)
}

/// Block until the backend answers its readiness probe, or the timeout elapses. In HTTPS
/// mode this waits for the cert file to appear, then probes with the pinned agent.
fn wait_healthy(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;

    // BOTH schemes, guess first. The Core decides this from its own database,
    // which this shell cannot read; asking the wrong one and declaring the Core
    // dead is the bug this loop exists to make impossible. Whichever answers is
    // recorded in LIVE_SCHEME, so every later URL -- the webview, the tray
    // poller, the alert poller -- is built from the socket's answer rather than
    // from the guess.
    let first = guessed_scheme();
    let second = if first == "https" { "http" } else { "https" };
    let mut agent: Option<ureq::Agent> = None;

    while Instant::now() < deadline {
        for candidate in [first, second] {
            // /healthz is token-exempt (see backend/wavr/app.py's
            // _TOKEN_EXEMPT_PATHS); /api/state is scope-gated, so probing it
            // would 401 forever when WAVR_LOCAL_TOKEN is set.
            let probe = format!("{}://127.0.0.1:{}/healthz", candidate, port());
            let answered = if candidate == "https" {
                // The backend writes cert.pem before uvicorn binds, so once the
                // port answers the cert exists. Pinned exactly, never trust-all:
                // a probe that accepts any certificate is not a check.
                if agent.is_none() {
                    if let Some(der) = pinned_cert_der() {
                        agent = Some(pinned_https_agent(der));
                    }
                }
                match &agent {
                    Some(a) => a.get(&probe).call().is_ok(),
                    None => false,
                }
            } else {
                ureq::get(&probe).timeout(Duration::from_secs(5)).call().is_ok()
            };
            if answered {
                remember_scheme(candidate);
                if candidate != first {
                    log_issue(&format!(
                        "Wavr: the Core is serving {candidate}, not the {first} this \
                         shell expected from its environment -- using {candidate}. \
                         (The switch was probably changed in Settings, which stores \
                         it in the Core's own database.)"));
                }
                return true;
            }
        }
        std::thread::sleep(Duration::from_millis(400));
    }
    false
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

/// Filename the backend persists the auto-generated token under (see
/// `backend/wavr/local_token.py::_TOKEN_FILENAME`) when `WAVR_LOCAL_TOKEN=auto`.
const LOCAL_TOKEN_FILENAME: &str = "local_token";

/// Mirrors `wavr.local_token._token_path()`: the persisted auto-token lives next to the
/// db file (`WAVR_DB`, default `wavr.db`), resolved relative to the SAME directory the
/// backend itself resolves it from -- `WAVR_BACKEND_DIR` if set (that's what
/// `spawn_backend()` passes as the child's `current_dir()`), else this process's own cwd
/// (which the child then inherits, Rust's `Command` default when `current_dir()` is never
/// called). `:memory:`/empty mirrors the Python side's `Path.cwd()` fallback.
fn local_token_file_path() -> PathBuf {
    let base = std::env::var("WAVR_BACKEND_DIR")
        .map(PathBuf::from)
        .or_else(|_| std::env::current_dir())
        .unwrap_or_default();
    let db_path = effective("WAVR_DB")
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "wavr.db".to_string());
    let dir = if db_path == ":memory:" {
        base
    } else {
        let p = Path::new(&db_path);
        let joined = if p.is_absolute() { p.to_path_buf() } else { base.join(p) };
        joined.parent().map(Path::to_path_buf).unwrap_or(base)
    };
    dir.join(LOCAL_TOKEN_FILENAME)
}

/// Resolve `WAVR_LOCAL_TOKEN` the same way `wavr.local_token.resolve_local_token()` does:
/// unset/empty -> "" (disabled); `auto` -> read the token the backend already generated
/// and persisted at `local_token_file_path()` (the backend resolves this at app-creation
/// time, BEFORE uvicorn ever answers `/healthz` -- see `app.py`'s `resolve_local_token()`
/// call happening inside `create_app()`, well before `serve.py`'s `uvicorn.run()` -- so by
/// the time `wait_healthy()` returns true the file is guaranteed to already exist);
/// anything else -> used verbatim. Without this, "auto" mode would send the literal
/// string `"auto"` as the token on every call below, which can never match the backend's
/// real generated secret. Read fresh each call (no caching), same reasoning as
/// `pinned_cert_der()`'s re-read-every-time. This shell only ever READS that file --
/// only the backend generates/persists it.
fn resolved_local_token() -> String {
    let cfg = effective("WAVR_LOCAL_TOKEN").unwrap_or_default().trim().to_string();
    if cfg.is_empty() {
        return cfg;
    }
    if cfg.eq_ignore_ascii_case("auto") {
        return std::fs::read_to_string(local_token_file_path())
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
    }
    cfg
}

/// One GET /api/alerts round-trip, using the same http/https + pinned-cert-agent split as
/// `wait_healthy()`. `https_agent` is cached across calls (lazily built once the cert is
/// readable) so we are not re-doing a TLS handshake setup on every poll tick.
fn fetch_alerts(url: &str, https_agent: &mut Option<ureq::Agent>) -> Option<Vec<serde_json::Value>> {
    // /api/alerts is scope-gated (NOT token-exempt like /healthz), so when
    // WAVR_LOCAL_TOKEN is set the backend requires it even on loopback. Send it via
    // X-Wavr-Token -- the header app.py's token middleware reads. resolved_local_token()
    // mirrors wavr.local_token.resolve_local_token() (including WAVR_LOCAL_TOKEN=auto);
    // empty = disabled.
    let token = resolved_local_token();
    let text = if scheme() == "https" {
        if https_agent.is_none() {
            *https_agent = pinned_cert_der().map(pinned_https_agent);
        }
        let mut req = https_agent.as_ref()?.get(url);
        if !token.is_empty() {
            req = req.set("X-Wavr-Token", &token);
        }
        req.call().ok()?.into_string().ok()?
    } else {
        let mut req = ureq::get(url).timeout(Duration::from_secs(5));
        if !token.is_empty() {
            req = req.set("X-Wavr-Token", &token);
        }
        req.call().ok()?.into_string().ok()?
    };
    let parsed: serde_json::Value = serde_json::from_str(&text).ok()?;
    parsed.get("alerts")?.as_array().cloned()
}

/// Fetch `GET /api/runtime` -- the Core's own conclusion about whether it is working.
///
/// Returns `None` for EVERY failure, and the caller must render "not responding" rather
/// than keeping the last good answer. That is the whole point: a tray that stays green
/// because the last poll succeeded is exactly the invisible failure this feature exists to
/// prevent. A Core that died an hour ago must look dead within one poll interval.
fn fetch_runtime(url: &str, https_agent: &mut Option<ureq::Agent>) -> Option<serde_json::Value> {
    // Scope-gated like /api/alerts, so it needs X-Wavr-Token when one is configured.
    let token = resolved_local_token();
    let text = if scheme() == "https" {
        if https_agent.is_none() {
            *https_agent = pinned_cert_der().map(pinned_https_agent);
        }
        let mut req = https_agent.as_ref()?.get(url);
        if !token.is_empty() {
            req = req.set("X-Wavr-Token", &token);
        }
        req.call().ok()?.into_string().ok()?
    } else {
        let mut req = ureq::get(url).timeout(Duration::from_secs(5));
        if !token.is_empty() {
            req = req.set("X-Wavr-Token", &token);
        }
        req.call().ok()?.into_string().ok()?
    };
    serde_json::from_str(&text).ok()
}

/// The tray's own copy of the runtime vocabulary, kept deliberately thin.
///
/// The BACKEND decides what state Wavr is in -- `wavr/runtime_status.py` -- and this only
/// renders it. Two implementations of "is it healthy" eventually disagree in front of
/// somebody who has no way to tell which is right, and the one on the tray is the one they
/// will believe, because it is the one they can see without opening anything.
/// Every id the tray menu uses, in the order they appear.
///
/// The builder and the click handler used to agree by hand. That works until
/// somebody adds an item and forgets the arm — and the symptom is a menu entry
/// that does nothing at all when clicked, which is indistinguishable from the
/// app having frozen. `tray_action` maps the list to behaviour and a test
/// asserts the mapping is total.
const TRAY_ITEMS: &[&str] = &[
    "status", "open", "attention", "privacy", "autostart", "restart", "quit",
];

/// What clicking a tray id does. `Status` is the disabled line at the top: it
/// is deliberately inert, and saying so here is what stops it being mistaken
/// for a missing handler.
#[derive(Debug, PartialEq, Eq)]
enum TrayAction {
    Inert,
    Open,
    OpenAt(&'static str),
    ToggleAutostart,
    RestartCore,
    Quit,
}

fn tray_action(id: &str) -> Option<TrayAction> {
    Some(match id {
        "status" => TrayAction::Inert,
        "open" => TrayAction::Open,
        // Both open the dashboard at the surface that answers the question. The
        // tray deliberately does not reimplement either: a second place to read
        // the same state is a second place for it to be wrong.
        //
        // These are fragments the shell now routes on. They used to name a hash
        // nothing read, so the items reloaded the dashboard on its default tab
        // and looked, to whoever clicked them, like nothing had happened.
        "attention" => TrayAction::OpenAt("#tab-inicio"),
        "privacy" => TrayAction::OpenAt("#gearSecTrust"),
        "autostart" => TrayAction::ToggleAutostart,
        "restart" => TrayAction::RestartCore,
        "quit" => TrayAction::Quit,
        _ => return None,
    })
}

struct TrayView {
    tooltip: String,
    /// One line for the status item in the menu. Never a metric on its own: "8/9" needs a
    /// person to know whether nine is a lot.
    summary: String,
}

fn tray_view(status: Option<&serde_json::Value>) -> TrayView {
    let Some(body) = status else {
        // Unreachable. Deliberately NOT the last known state.
        //
        // Both strings are the Core's own, verbatim: `runtime_status.HEADLINE_SILENT`
        // and `runtime_status.CORE_SILENT`. `unreachable()` exists so "a tray, a menu
        // bar and a browser tab cannot disagree about what 'I got no answer' means",
        // and this tray is Rust, so it cannot call it -- it can only copy it and be
        // checked. It said "Wavr is not answering. It may have stopped." while the
        // browser chip said "...on this machine...", which is the drift that docstring
        // promised was impossible. `test_vocabulary.py` compares the three now.
        return TrayView {
            tooltip: "Wavr — not responding".to_string(),
            summary: "Wavr is not answering on this machine. It may have stopped."
                .to_string(),
        };
    };
    let headline = body
        .get("headline")
        .and_then(|h| h.as_str())
        .unwrap_or("Wavr");
    let state = body.get("state").and_then(|s| s.as_str()).unwrap_or("");

    // The summary names the worst thing rather than counting the good ones. A menu line
    // reading "3 of 4 fine" is read as fine.
    let worst = body
        .get("findings")
        .and_then(|f| f.as_array())
        .and_then(|rows| {
            rows.iter()
                .find(|r| r.get("state").and_then(|s| s.as_str()) == Some(state))
        })
        .and_then(|r| r.get("text"))
        .and_then(|t| t.as_str())
        .unwrap_or("");

    let summary = match state {
        "healthy" => "Everything looks good".to_string(),
        "" => "Wavr".to_string(),
        // The Core's own sentence whenever there is one: it knows WHY, and a
        // surface that paraphrases it has become a second producer.
        _ if !worst.is_empty() => worst.to_string(),
        // No finding to borrow -- and this fell through to `other.to_string()`,
        // which put the wire value on a menu line. "starting" happened on every
        // single launch, before the Core had assessed anything, so the first
        // thing this product said to somebody was an identifier. Each arm below
        // is the word `docs/VOCABULARY.md` already fixes for that state, in this
        // surface's own idiom (a line, not a chip label). Rendering a word for a
        // state the Core chose is not deciding the state.
        "starting" => "Starting — no reading yet".to_string(),
        "updating" => "An update is in progress".to_string(),
        "paused" => "Paused — deliberately not watching".to_string(),
        "degraded" => "Working, less well — something to fix".to_string(),
        "attention" => "Needs attention".to_string(),
        "unavailable" => "Not responding".to_string(),
        other => other.to_string(),
    };

    // The tooltip is two words, not the headline.
    //
    // It used to be the Core's full headline, which is the right sentence in the
    // wrong place: a tooltip appears for half a second under the cursor, in the
    // corner of the screen, and answers exactly one question -- is this alive?
    // The first person to hover it said so plainly: "dá um monte de informações,
    // seria melhor aparecer só Wavr - Live".
    //
    // The detail did not go anywhere. It is on the MENU LINE below, which is
    // open, has room, and still carries the Core's own sentence including the
    // reason when there is one.
    let word = match state {
        "healthy" => "live",
        "starting" => "starting",
        "updating" => "updating",
        "paused" => "paused",
        "degraded" => "needs a look",
        "attention" => "needs attention",
        "unavailable" => "not responding",
        "" => "",
        other => other,
    };
    let tooltip = if word.is_empty() {
        "Wavr".to_string()
    } else {
        format!("Wavr \u{2014} {word}")
    };
    let _ = headline;   // kept above for the summary's sake; not the tooltip's
    TrayView { tooltip, summary }
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

/// Item 2 (crash recovery): detect the backend child dying AFTER the initial
/// `wait_healthy()` already succeeded (a Python traceback, an OOM kill, someone
/// `taskkill`ing this specific pid, ...) and attempt ONE respawn before giving up -- so an
/// unexpected backend death doesn't silently wedge the app on a dashboard that can no
/// longer reach anything, with zero explanation or recourse. Only ever called after the
/// INITIAL `wait_healthy()` already returned true (see the call site in `setup()`), so
/// there is always a live child in `Backend` when this starts polling.
///
/// Bounded, not an unbounded supervisor loop -- `restarted_once` caps this at exactly one
/// respawn attempt ever, so a backend that keeps crashing can't trigger a restart storm.
/// `kill_backend()` (tray Quit / `RunEvent::ExitRequested`) always takes the child OUT of
/// the `Mutex` (sets it to `None`) BEFORE killing it, so a deliberate shutdown is
/// indistinguishable from "nothing left to monitor" here -- the poll loop simply stops the
/// moment it observes `None`, same as it would for any other reason to stop watching. A
/// respawned child is re-confined to its own (Windows-only) Job Object exactly like the
/// first one -- see `confine_backend_to_job_object()`'s doc comment: each call's job
/// outlives this process independently, so the crash-safety net still holds even if this
/// second child later also needs a forced kill.
fn spawn_backend_monitor(app: tauri::AppHandle) {
    const POLL_INTERVAL: Duration = Duration::from_secs(5);
    std::thread::spawn(move || {
        let mut restarted_once = false;
        loop {
            std::thread::sleep(POLL_INTERVAL);

            let exited = {
                let state = app.state::<Backend>();
                let mut guard = state.0.lock().unwrap();
                match guard.as_mut() {
                    None => return, // deliberate shutdown already took it -- stop watching
                    Some(child) => matches!(child.try_wait(), Ok(Some(_))),
                }
            };
            if !exited {
                continue;
            }
            *app.state::<Backend>().0.lock().unwrap() = None; // already dead, nothing to kill

            if restarted_once {
                report_backend_crashed(
                    &app,
                    "Wavr backend exited unexpectedly and the automatic restart also did not \
                     become healthy. Restart Wavr Desktop manually, or check \
                     ~/.wavr/desktop.log.",
                );
                return;
            }
            restarted_once = true;
            log_issue("Wavr: backend process exited unexpectedly -- attempting one restart");

            match spawn_backend() {
                Ok(child) => {
                    // Confined and resumed by spawn_backend() before it returned.
                    *app.state::<Backend>().0.lock().unwrap() = Some(child);
                    if wait_healthy(HEALTH_TIMEOUT) {
                        log_issue("Wavr: backend restarted and is healthy again");
                        if let Some(w) = app.get_webview_window("main") {
                            if let Ok(u) = backend_url().parse::<tauri::Url>() {
                                let _ = w.navigate(u);
                            }
                        }
                        // Keep polling: a second crash of this new child still hits the
                        // `restarted_once` branch above and gives up cleanly.
                    } else {
                        report_backend_crashed(
                            &app,
                            "Wavr backend exited unexpectedly and the restart did not become \
                             healthy in time. Restart Wavr Desktop manually, or check \
                             ~/.wavr/desktop.log.",
                        );
                        return;
                    }
                }
                Err(e) => {
                    report_backend_crashed(
                        &app,
                        &format!(
                            "Wavr backend exited unexpectedly and could not be restarted: {e}. \
                             Restart Wavr Desktop manually, or check ~/.wavr/desktop.log."
                        ),
                    );
                    return;
                }
            }
        }
    });
}

/// Native-notification + in-webview-banner surfacing for `spawn_backend_monitor()`'s
/// give-up path. Reuses the same `window.wavrShowStartupError` hook and "sanctioned
/// `eval()`, Rust-authored + JSON-escaped `msg`" reasoning as the initial-boot timeout
/// path in `setup()` below.
fn report_backend_crashed(app: &tauri::AppHandle, msg: &str) {
    log_issue(&format!("Wavr: {msg}"));
    // The in-page banner that used to be attempted here is gone, and why is
    // worth keeping: it never once rendered.
    //
    // It called `window.wavrShowStartupError`, which is defined only in the
    // bundled placeholder page (`desktop/dist/index.html`) and never in the
    // live dashboard the Core serves. This function is reached from the crash
    // monitor, and that monitor by construction only runs AFTER the window has
    // navigated to the live dashboard — so the `&&` guard was false every
    // time. A dead branch that reads like a feature is worse than no branch:
    // it is how "the desktop shell shows nothing when the Core dies" survived
    // review, twice.
    //
    // Nothing is lost, because the dashboard already owns this state and owns
    // it well: the runtime chip turns red and reads "Not responding", the
    // headline gains "reconnecting…", and every tile that depends on a reading
    // dims and says it may be out of date.
    // `test_the_desktop_shell_has_nothing_left_to_say.py` holds that — if the
    // dashboard ever stops announcing a dead Core, a test fails instead of a
    // user finding out.
    //
    // What a shell CAN add is below: an OS notification, which is the one
    // thing a web page cannot do for a window nobody is looking at.
    if notifications_enabled() {
        let _ = app
            .notification()
            .builder()
            .title("Wavr backend stopped")
            .body(msg)
            .show();
    }
}

fn kill_backend(app: &tauri::AppHandle) {
    // The Job Object first, because the tracked child is not the whole Core.
    //
    // PyInstaller onefile runs a BOOTLOADER that unpacks and then spawns the real
    // worker as its own child. `Child::kill()` reaches only the bootloader; the
    // worker survives, keeps listening on the port, and the next spawn silently
    // fails to bind. That is what the first user hit: "Restart Core" logged
    // success twice while the original process kept answering with the original
    // configuration.
    //
    // TerminateJobObject takes the whole tree at once, which is precisely what
    // this job was created to be able to do.
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::{CloseHandle, HANDLE};
        use windows::Win32::System::JobObjects::TerminateJobObject;
        let taken = BACKEND_JOB.lock().ok().and_then(|mut s| s.take());
        if let Some(raw) = taken {
            let job = HANDLE(raw as *mut std::ffi::c_void);
            unsafe {
                if TerminateJobObject(job, 0).is_err() {
                    log_issue("Wavr: TerminateJobObject failed; falling back to killing \
                               the tracked child only (a leftover Core may keep the port)");
                }
                let _ = CloseHandle(job);
            }
        }
    }
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
            .map_err(|e| format!("Wavr: AssignProcessToJobObject failed (no crash-safety net for the sidecar): {e}"))?;
        // Remembered so `kill_backend` can terminate the whole tree deliberately.
        // Without this the tray's "Restart Core" killed only the tracked child --
        // the PyInstaller BOOTLOADER -- while the worker it had spawned kept the
        // port, and the replacement Core could never bind. The menu said it had
        // restarted; nothing had.
        if let Ok(mut slot) = BACKEND_JOB.lock() {
            *slot = Some(job.0 as usize);
        }
        Ok(())
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
/// Open the dashboard AND put it on the surface that answers the question.
///
/// The tray points at the dashboard rather than reimplementing anything. A second place
/// to read the same state is a second place for it to be wrong, and the one people can see
/// without opening anything is the one they will believe.
fn open_at(app: &tauri::AppHandle, fragment: &str) {
    show_window(app);
    if let Some(w) = app.get_webview_window("main") {
        let target = format!("{}/{}", backend_url(), fragment);
        if let Ok(u) = target.parse::<tauri::Url>() {
            let _ = w.navigate(u);
        }
    }
}

/// Stop the Core and start it again, without quitting Wavr.
///
/// The recovery path for the state the tray exists to make visible: a Core that has
/// stopped producing. Killing and respawning is the honest implementation — the backend
/// has no in-process restart, and pretending otherwise would leave a "restarted" message
/// over an unchanged process.
///
/// `spawn_backend_monitor` watches the handle in `Backend`, but only for a process that
/// EXITS. A Core that starts and never answers is invisible to it — which is the state
/// this function used to leave behind — so the wait below is this path's own.
fn restart_backend(app: &tauri::AppHandle) {
    kill_backend(app);
    match spawn_backend() {
        Ok(child) => {
            if let Some(state) = app.try_state::<Backend>() {
                *state.0.lock().unwrap() = Some(child);
            }
            // Say what happened, not what was attempted.
            //
            // This line read "Wavr: Core restarted from the tray." and was written
            // the instant the spawn call returned, before anything had started.
            // It has now been wrong twice in one day for two different reasons —
            // first over a Core whose worker still held the port, then over a Core
            // left suspended and never resumed — and both times the log said the
            // restart had happened while the dashboard sat on "reconnecting...".
            // A success message that cannot fail is not a message.
            if wait_healthy(HEALTH_TIMEOUT) {
                // Point the window at the Core that is answering NOW, exactly as the
                // crash watchdog does. Two paths bring a Core back and only one of
                // them was telling the window about it, so a restart from the menu
                // left the page sitting on whatever it had -- and if the scheme had
                // changed while it was down, sitting there forever. `backend_url()`
                // is read after `wait_healthy`, so it carries the scheme the socket
                // just answered on rather than the one this process guessed at start.
                if let Some(w) = app.get_webview_window("main") {
                    if let Ok(u) = backend_url().parse::<tauri::Url>() {
                        let _ = w.navigate(u);
                    }
                }
                log_issue("Wavr: Core restarted from the tray and is answering.");
            } else {
                log_issue(
                    "Wavr: Core was restarted from the tray but is NOT answering. \
                     Quit Wavr from this menu and open it again; if that does not \
                     help, see ~/.wavr/desktop.log.",
                );
                report_backend_crashed(
                    app,
                    "Wavr restarted the Core, but it did not start answering. Quit Wavr \
                     from the tray menu and open it again.",
                );
            }
        }
        Err(e) => log_issue(&format!("Wavr: could not restart the Core: {e}")),
    }
}

/// Keep the tray telling the truth, on a timer.
///
/// Every tick asks the Core what it thinks of itself and renders that. A failed request is
/// rendered as "not responding" rather than leaving the previous answer in place: a tray
/// that stays green because the last poll succeeded is precisely the invisible failure
/// this whole surface exists to prevent.
///
/// Deliberately quiet. The tooltip and the menu line change; nothing is notified. A
/// notification belongs to a transition that needs a person to act, and this ticks every
/// few seconds forever.
fn spawn_runtime_presence(
    app: tauri::AppHandle,
    tray: tauri::tray::TrayIcon,
    status_item: tauri::menu::MenuItem<tauri::Wry>,
) {
    std::thread::spawn(move || {
        const POLL_INTERVAL: Duration = Duration::from_secs(10);
        let mut https_agent: Option<ureq::Agent> = None;
        let mut last_tooltip = String::new();

        loop {
            // Built per tick, not once. This thread starts before the health
            // probe runs, so a URL frozen here keeps the GUESS for the life of
            // the process -- and on a Core serving https the tray then reports
            // "Wavr is not answering on this machine" for ever, beside a window
            // that is working. `fetch_runtime` already re-reads `scheme()` for
            // its own branch, so the URL and the branch used to disagree.
            let url = format!("{}/api/runtime", backend_url());
            let body = fetch_runtime(&url, &mut https_agent);
            let view = tray_view(body.as_ref());

            // Only touch the OS when something changed: a tray icon rewritten every ten
            // seconds is a wakeup every ten seconds on a machine that may be a laptop.
            if view.tooltip != last_tooltip {
                let _ = tray.set_tooltip(Some(&view.tooltip));
                let _ = status_item.set_text(&view.summary);
                last_tooltip = view.tooltip.clone();
            }
            let _ = &app;
            std::thread::sleep(POLL_INTERVAL);
        }
    });
}

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
                // Already confined to a Job Object and resumed by spawn_backend().
                Ok(child) => *app.state::<Backend>().0.lock().unwrap() = Some(child),
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

            // 3. reconcile the login-item state every launch. The env var is the
            //    DEFAULT; a choice made in the tray is recorded and wins (see
            //    `autostart_enabled`). It used to be purely declarative from
            //    WAVR_DESKTOP_AUTOSTART with no UI at all — which meant a person
            //    had no way to see the setting, let alone change it, and any
            //    toggle we added would have been undone on the next launch.
            //    Default is still OFF: Wavr does not add itself to startup
            //    without being asked.
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

            // 4. tray icon + menu: the RUNTIME PRESENCE surface.
            //
            // NO INVISIBLE SUCCESS. If Wavr is watching somebody's home, that person must
            // be able to tell without Task Manager, a terminal or a log file. The tray is
            // the only place most people will ever look, so it carries the Core's real
            // conclusion rather than "the process I spawned has not exited".
            //
            // The first item is a disabled status line, not an action. It exists so the
            // answer is visible the moment the menu opens, before anybody has to know
            // which item to click.
            let status = MenuItemBuilder::with_id("status", "Wavr — starting…")
                .enabled(false)
                .build(app)?;
            let open = MenuItemBuilder::with_id("open", "Open Wavr").build(app)?;
            let attention =
                MenuItemBuilder::with_id("attention", "Needs attention").build(app)?;
            let privacy = MenuItemBuilder::with_id("privacy", "Privacy").build(app)?;
            let restart =
                MenuItemBuilder::with_id("restart", "Restart Core").build(app)?;
            // Ticked from the OS's own answer, not from what we asked for. A
            // login item that failed to register, under a tick that says it
            // worked, is the same class of lie as a green icon over a dead Core.
            let starts_with_machine = app
                .autolaunch()
                .is_enabled()
                .unwrap_or_else(|_| autostart_enabled());
            let autostart = CheckMenuItemBuilder::with_id(
                "autostart", "Start Wavr when this machine starts")
                .checked(starts_with_machine)
                .build(app)?;
            // Named "Quit Wavr", never bare "Quit". Closing the WINDOW leaves Wavr
            // sensing; this stops it. Two different outcomes must not share one word.
            let quit = MenuItemBuilder::with_id("quit", "Quit Wavr").build(app)?;
            // The order here IS `TRAY_ITEMS`, checked rather than trusted. A
            // list nothing consults couples nothing — this is what makes
            // "every item has an action" a real guarantee instead of a comment.
            debug_assert_eq!(
                TRAY_ITEMS,
                &["status", "open", "attention", "privacy", "autostart",
                  "restart", "quit"],
                "the tray menu and TRAY_ITEMS have diverged",
            );
            let menu = MenuBuilder::new(app)
                .items(&[&status, &open, &attention, &privacy, &autostart,
                         &restart, &quit])
                .build()?;
            let tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Wavr — starting…")
                .menu(&menu)
                .on_menu_event(|app, event| {
                    match tray_action(event.id().as_ref()) {
                        Some(TrayAction::Inert) | None => {}
                        Some(TrayAction::Open) => show_window(app),
                        Some(TrayAction::OpenAt(fragment)) => open_at(app, fragment),
                        Some(TrayAction::ToggleAutostart) => {
                            // Read the CURRENT state and invert it, rather than
                            // trusting the tick we last drew — the menu item and
                            // the OS can disagree, and the OS is right.
                            let now_on =
                                app.autolaunch().is_enabled().unwrap_or(false);
                            set_autostart(app, !now_on);
                        }
                        Some(TrayAction::RestartCore) => {
                            // Off the event loop. `restart_backend` waits for
                            // health, which can take the full HEALTH_TIMEOUT,
                            // and this closure runs ON the main thread: doing
                            // it here freezes the window and the tray menu for
                            // 45 seconds, in precisely the situation the button
                            // exists for -- a Core that is not coming back.
                            let handle = app.clone();
                            std::thread::spawn(move || restart_backend(&handle));
                        }
                        Some(TrayAction::Quit) => {
                            kill_backend(app);
                            app.exit(0);
                        }
                    }
                })
                .build(app)?;

            // Keep the tray honest on a timer. `status_item` is updated in place so the
            // menu reads correctly whenever it is opened, rather than only after a click.
            spawn_runtime_presence(app.handle().clone(), tray, status);

            // 5. Install the scoped cert pin BEFORE any navigation happens. Always,
            //    not "if multidevice()".
            //
            //    `multidevice()` reads WAVR_MULTIDEVICE from the environment or a
            //    `.env` file. An installed Core has neither: the switch a person
            //    turns on in Settings lives in the CORE'S OWN DATABASE, which this
            //    shell cannot read. So on the machine of the first person to install
            //    Wavr and turn that switch on, this condition was false, the pin was
            //    never armed, and the window -- correctly navigated to https by then
            //    -- was met by "Your connection isn't private" with a Continue
            //    (unsafe) link under it.
            //
            //    The gate never bought any safety. The handler is scoped by itself:
            //    it ALLOWS only a certificate byte-identical to the one on disk, at
            //    exactly 127.0.0.1:<port>, and CANCELs everything else -- including
            //    the case where there is no certificate at all, which is what a
            //    plain-HTTP Core looks like. Installing it unconditionally is
            //    strictly safer than not installing it, because the alternative is
            //    WebView2's own interstitial, which offers the person a button that
            //    clicks straight through a real interception.
            //
            //    Third consumer of the same fact to be caught guessing it today. The
            //    probe discovers the scheme; the navigation now reads what the probe
            //    found; and this no longer asks the question at all.
            #[cfg(windows)]
            install_cert_pinning(app.handle());

            // 6. once the backend is healthy, navigate the window to it (done off-thread so
            //    setup() returns immediately and the "Starting…" placeholder shows
            //    meanwhile, when the window is shown at all) and start the native-alert
            //    poller (item 4). Item 3: if the backend NEVER becomes healthy (wrong
            //    WAVR_PYTHON, a missing dep, a zombie process already holding the port --
            //    all seen in this repo's history), surface that instead of leaving the
            //    window stuck on "Starting Wavr…" forever with no recourse.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if wait_healthy(HEALTH_TIMEOUT) {
                    // Read AFTER the probe, and that is the whole point of the line.
                    //
                    // It used to be read one line above the `if`, so the window was
                    // navigated to `guessed_scheme()` -- the exact value `wait_healthy`
                    // exists to replace. Against a Core serving HTTPS the window opened
                    // `http://127.0.0.1:8000`; every request from that page failed
                    // forever, and the service worker answered the navigation out of its
                    // offline cache. So a dashboard appeared, fully drawn, and filled
                    // its empty state with the built-in sample house: three rooms the
                    // person does not have, under a Space named "Command Center",
                    // beside a small chip reading "reconnecting".
                    //
                    // The morning's fix taught the PROBE to try both schemes and record
                    // the winner. It did not teach the navigation to read it. One
                    // address, two sources, a second time.
                    let url = backend_url();
                    if let Some(w) = handle.get_webview_window("main") {
                        if let Ok(u) = url.parse::<tauri::Url>() {
                            let _ = w.navigate(u);
                        }
                    }
                    spawn_alert_notifier(handle.clone());
                    // Item 2: now that the FIRST health check has passed and a live child
                    // is in `Backend`, start watching for it dying later (see
                    // spawn_backend_monitor()'s doc comment for why it's safe to start
                    // exactly here and not before).
                    spawn_backend_monitor(handle.clone());
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
                    // Names the PORT, not one scheme. `wait_healthy` probes http and
                    // https both, so "did not answer at http://..." was already half a
                    // sentence -- and it was the half that sent me looking in the wrong
                    // place this morning, when the Core was answering perfectly well on
                    // the other one.
                    // Says what is true at this instant AND what is still happening.
                    // The previous version read as a verdict — "did not answer,
                    // check these three things" — while a thread behind it was
                    // still watching, and on the machine that produced this message
                    // the Core answered three seconds later.
                    let msg = format!(
                        "Wavr hasn't answered on port {} yet (tried http and https for \
                         {}s). Still watching — if it comes up, this page opens on its \
                         own. A first start after installing can be slow while Windows \
                         scans the new file. If it stays like this, check that no \
                         leftover Wavr is already holding the port, and see \
                         ~/.wavr/desktop.log.",
                        port(),
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
                            // Same correction as the on-screen message: this fired
                            // while the shell was still watching, and said "never".
                            .title("Wavr is taking longer than usual")
                            .body(
                                "The Core hasn't answered yet. Wavr is still watching and \
                                 will open by itself if it comes up.",
                            )
                            .show();
                    }

                    // KEEP WATCHING. A timeout is not a verdict.
                    //
                    // Measured on the first user's machine: the shell started at
                    // 21:01:51, the Core's worker began answering at 21:02:28, and
                    // the wait gave up at 21:02:25 — three seconds early. The Core
                    // then served happily for the next minute and a half while the
                    // window sat on "Wavr didn't start" and nothing in the shell was
                    // looking any more.
                    //
                    // Raising the number would only move the cliff. What was wrong is
                    // that the first answer was treated as the last one, on the one
                    // path where the thing being waited for is a 40MB self-extracting
                    // binary that Windows may be scanning for the first time.
                    //
                    // So the message above stands as the honest report of the wait,
                    // and this keeps probing quietly behind it. If the Core does come
                    // up, the window goes where it should have gone and the error
                    // stops being on screen. If it never does, nothing further is
                    // said — this loop is bounded and silent.
                    let tardio = handle.clone();
                    std::thread::spawn(move || {
                        if !wait_healthy(LATE_START_GRACE) {
                            log_issue(
                                "Wavr: the Core never answered, including the grace \
                                 period after the startup wait. Leaving the failure \
                                 message on screen.",
                            );
                            return;
                        }
                        log_issue(
                            "Wavr: the Core answered after the startup wait had already \
                             given up -- opening the dashboard.",
                        );
                        let url = backend_url();
                        if let Some(w) = tardio.get_webview_window("main") {
                            if let Ok(u) = url.parse::<tauri::Url>() {
                                let _ = w.navigate(u);
                            }
                        }
                        spawn_alert_notifier(tardio.clone());
                        spawn_backend_monitor(tardio.clone());
                    });
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

// ---------------------------------------------------------------------------
// Tests
//
// Only the pure rendering is tested here. Spawning a Core, driving a tray and
// clicking a menu need a desktop session and a running backend, and those are
// covered by the packaged-application checks rather than pretended at here.
//
// What IS tested is the rule the whole surface rests on: an unreachable Core
// must never render as a healthy one. That is a pure function of the response,
// which makes it exactly the part worth pinning.
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::tray_view;

    fn body(json: &str) -> serde_json::Value {
        serde_json::from_str(json).expect("test fixture is valid json")
    }

    #[test]
    fn no_answer_renders_as_not_responding_and_never_as_healthy() {
        // The failure this surface exists to prevent: the last poll succeeded,
        // the Core has since died, and the icon is still green.
        let view = tray_view(None);
        assert!(view.tooltip.contains("not responding"), "{}", view.tooltip);
        assert!(!view.tooltip.to_lowercase().contains("healthy"));
        assert!(view.summary.contains("not answering"), "{}", view.summary);
    }

    #[test]
    fn a_healthy_core_says_so_in_a_sentence_not_a_metric() {
        let view = tray_view(Some(&body(
            r#"{"state":"healthy","headline":"Wavr — running · My Home · everything reporting","findings":[]}"#,
        )));
        assert!(view.tooltip.contains("My Home"));
        assert_eq!(view.summary, "Everything looks good");
    }

    #[test]
    fn a_degraded_core_puts_the_worst_finding_on_the_menu_line() {
        // "3 of 4 fine" is read as fine. The line must name the problem.
        let view = tray_view(Some(&body(
            r#"{"state":"degraded",
                "headline":"Wavr — degraded · My Home",
                "findings":[{"state":"healthy","text":"2 sensors reporting."},
                            {"state":"degraded","text":"Kitchen radar is not reporting."}]}"#,
        )));
        assert_eq!(view.summary, "Kitchen radar is not reporting.");
        assert!(view.tooltip.contains("degraded"));
    }

    #[test]
    fn every_tray_item_has_an_action() {
        // The failure this prevents: somebody adds a menu entry and forgets the
        // handler arm. The symptom is an item that does nothing at all when
        // clicked, which a person cannot tell apart from the app having frozen.
        for id in super::TRAY_ITEMS {
            assert!(
                super::tray_action(id).is_some(),
                "tray item {id:?} has no action",
            );
        }
    }

    #[test]
    fn an_unknown_id_is_ignored_rather_than_guessed() {
        assert!(super::tray_action("definitely-not-an-item").is_none());
    }

    #[test]
    fn the_status_line_is_deliberately_inert_rather_than_unhandled() {
        // It is the disabled line at the top of the menu. Saying so explicitly
        // is what stops it looking like a handler somebody forgot.
        assert_eq!(super::tray_action("status"), Some(super::TrayAction::Inert));
    }

    #[test]
    fn the_two_navigating_items_name_fragments_the_shell_routes_on() {
        // These used to name a hash nothing read. The shell routes on `#tab-*`
        // and `#gearSec*`; anything else silently reloads the default tab.
        for id in ["attention", "privacy"] {
            match super::tray_action(id) {
                Some(super::TrayAction::OpenAt(f)) => assert!(
                    f.starts_with("#tab-") || f.starts_with("#gearSec"),
                    "{id:?} navigates to {f:?}, which the shell does not route on",
                ),
                other => panic!("{id:?} should navigate, got {other:?}"),
            }
        }
    }

    #[test]
    fn an_unparseable_or_empty_body_does_not_become_good_news() {
        let view = tray_view(Some(&body("{}")));
        assert_eq!(view.tooltip, "Wavr");
        assert_ne!(view.summary, "Everything looks good");
    }
}
