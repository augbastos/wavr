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
  server: {
    // Bundled assets served from https://localhost. No remote URL, no cleartext.
    androidScheme: 'https',
  },
};

export default config;
