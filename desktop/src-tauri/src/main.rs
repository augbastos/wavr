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

const PORT_ENV: &str = "WAVR_PORT";
const DEFAULT_PORT: &str = "8000";

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

fn multidevice() -> bool {
    effective("WAVR_MULTIDEVICE").map(|v| is_truthy(&v)).unwrap_or(false)
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
    ureq::builder().tls_config(Arc::new(config)).build()
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
            if ureq::get(&probe).call().is_ok() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(400));
        }
        false
    }
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        if let Some(mut child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
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

            // 3. HTTPS mode: install the scoped cert pin BEFORE any navigation happens.
            #[cfg(windows)]
            if multidevice() {
                install_cert_pinning(app.handle());
            }

            // 4. once the backend is healthy, navigate the window to it. Done off-thread so
            //    setup() returns immediately and the "Starting…" placeholder shows meanwhile.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let url = backend_url();
                if wait_healthy(Duration::from_secs(20)) {
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
