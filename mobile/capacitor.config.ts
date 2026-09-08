import type { CapacitorConfig } from '@capacitor/cli';

/**
 * Wavr Mobile — Capacitor config (Phase 1).
 *
 * appId mirrors the desktop Tauri precedent (dev.wavr.desktop -> dev.wavr.mobile).
 *
 * The app BUNDLES frontend/index.html (regenerated into ./www by `npm run
 * sync-frontend`) and serves it locally. There is DELIBERATELY no `server.url`:
 * the WebView never loads a remote page. Every request/socket to the user's LAN
 * central goes through the native WavrNet plugin (OkHttp with a TrustManager that
 * pins EXACTLY the one SHA-256 fingerprint captured at pairing — never trust-all,
 * never the system store as a fallback). The WebView itself makes no external calls.
 *
 * androidScheme 'https' => the bundled app is served from https://localhost, so
 * window.isSecureContext is true (service worker + secure APIs work) and there is
 * no cleartext origin anywhere in the app.
 */
const config: CapacitorConfig = {
  appId: 'dev.wavr.mobile',
  appName: 'Wavr',
  webDir: 'www',
  // Capacitor's OWN bridge (Bridge.java#callPluginMethod, gated by
  // Logger.shouldLog()) verbose-logs every plugin call's full argument data —
  // including our Authorization: Bearer <token> header passed into
  // WavrNet.request() — to logcat. That happens inside the framework, before
  // the WavrNet plugin runs, so plugin-side redaction cannot cover it. The
  // default 'debug' behavior only suppresses this in non-debuggable (release)
  // builds; every debug APK we side-load for on-device testing was leaking
  // the token. 'none' forces loggingEnabled=false unconditionally (CapConfig
  // LOG_BEHAVIOR_NONE), so Logger.shouldLog() is false in ALL build types —
  // defense-in-depth for the never-log-credentials invariant. Does NOT affect
  // WavrNet's own redacted W/WavrNet diagnostics: those call android.util.Log
  // directly, bypassing Capacitor's Logger/shouldLog() gate entirely.
  loggingBehavior: 'none',
  server: {
    // Bundled assets served from https://localhost. No remote URL, no cleartext.
    androidScheme: 'https',
  },
};

export default config;
