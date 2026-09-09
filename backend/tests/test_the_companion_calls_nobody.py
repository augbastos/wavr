"""The Android companion talks to the user's own hub and to nothing else.

The dashboard's "no external network request" invariant has had a producer for a
while: a test walks `frontend/` and fails on any external host. The COMPANION had
none — and it is the surface where the claim is hardest to keep, because an
Android app can reach the network from three places the web app cannot:

  * the WebView, if anything ever points it at a remote page;
  * a native plugin, in Kotlin, invisible to any check that reads JavaScript;
  * a dependency, which can phone home without a single line of project code.

An audit that reads only the first of those is the shape of audit that misses the
other two. This one reads all three, from the source that actually ships.

## What it does NOT do

It does not run the app and watch a proxy. Building a whole interception harness
to prove a negative would cost more than it returns, and it would still only
prove the negative for the paths the test happened to exercise.

So this is a static guard with a stated limit: it catches a URL, an SDK or a
configuration change that opens an egress path. It does not catch a host
assembled at runtime from pieces. That is the honest boundary, and it is written
here rather than left for a reader to discover.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOBILE = REPO / "mobile"

pytestmark = pytest.mark.skipif(
    not MOBILE.is_dir(),
    reason="mobile/ is absent from this checkout")


# Hosts a bundled app may legitimately name. `localhost` is where Capacitor
# serves the bundle from; the rest are the shapes of a private network.
LOCAL_HOSTS = re.compile(
    r"""(?xi) ^(?:
        localhost | 127\.0\.0\.1 | \[::1\] | 0\.0\.0\.0
      | (?:\d{1,3}\.){3}\d{1,3}
      | [\w.-]+\.local
    )$""")

URL = re.compile(r"""https?://([A-Za-z0-9._\[\]:-]+)""")

# Source that ships. `www/` is build output regenerated from `frontend/`, which
# has its own zero-external-request guard; `node_modules` and `build` are not ours.
def _shipped_sources():
    roots = [MOBILE / "src", MOBILE / "plugins", MOBILE / "scripts"]
    files = [MOBILE / "capacitor.config.ts", MOBILE / "package.json"]
    for root in roots:
        if root.is_dir():
            files += [p for p in root.rglob("*")
                      if p.is_file()
                      and p.suffix in {".js", ".mjs", ".ts", ".kt", ".java", ".json"}]
    android = MOBILE / "android" / "app" / "src" / "main"
    if android.is_dir():
        files += [p for p in android.rglob("*")
                  if p.is_file() and p.suffix in {".kt", ".java", ".xml"}]
    gradle = MOBILE / "android" / "app" / "build.gradle"
    if gradle.exists():
        files.append(gradle)
    return [p for p in files if p.exists()]


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


_BLOCK = re.compile(r"/\*.*?\*/|<!--.*?-->", re.S)
_LINE = re.compile(r"(?m)^\s*//.*$|\s//.*$")


def _code_only(text: str, suffix: str) -> str:
    """Comments blanked, line numbering preserved.

    A URL in a comment is documentation; a URL in code is an endpoint, and only
    the second one is a network path. The first version of this guard did not
    distinguish them and reported a documentation link in a Gradle comment and
    the end of an English sentence in a docstring -- exactly the false-blocker
    class that teaches people to stop reading the tool's output.

    Lines are blanked rather than removed so the line numbers in a finding still
    point at the right place in the file.
    """
    if suffix in {".json"}:
        return text                      # JSON has no comments
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = _BLOCK.sub(blank, text)
    if suffix in {".js", ".mjs", ".ts", ".kt", ".java", ".gradle"}:
        text = _LINE.sub(lambda m: " " * len(m.group(0)), text)
    return text


def test_the_companion_source_names_no_external_host():
    offenders = []
    for path in _shipped_sources():
        raw = path.read_text(encoding="utf-8", errors="replace")
        body = _code_only(raw, path.suffix)
        for lineno, line in enumerate(body.splitlines(), 1):
            for host in URL.findall(line):
                host = host.split(":")[0] if not host.startswith("[") else host
                if LOCAL_HOSTS.match(host):
                    continue
                # XML namespace declarations are identifiers, not endpoints:
                # nothing resolves schemas.android.com at runtime.
                if "xmlns" in line or host in {"schemas.android.com", "www.w3.org"}:
                    continue
                offenders.append(f"{_rel(path)}:{lineno} -> {host}")

    assert not offenders, (
        "the companion names a host outside the user's own network:\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery request to the hub goes through the native pinned client. A "
          "URL to anywhere else is either an egress path or a comment that reads "
          "like one; both need a decision, not a default.")


# SDKs whose entire purpose is to send data somewhere. Naming them beats a
# generic "network library" check, which would flag OkHttp — the pinned client
# the product is built on.
PHONE_HOME_SDKS = re.compile(
    r"""(?xi)
    firebase | crashlytics | google-analytics | play-services-analytics
  | com\.google\.android\.gms\.analytics
  | sentry | bugsnag | mixpanel | amplitude | segment\.analytics
  | appcenter | flurry | adjust\.sdk | com\.facebook\.android
  | onesignal | branch\.sdk | datadog | newrelic
""")


def test_no_dependency_exists_to_phone_home():
    gradle = MOBILE / "android" / "app" / "build.gradle"
    if not gradle.exists():
        pytest.skip("no app build.gradle")
    hits = []
    for lineno, line in enumerate(
            gradle.read_text(encoding="utf-8").splitlines(), 1):
        if not re.search(r"\b(?:implementation|api|compileOnly|runtimeOnly)\b", line):
            continue
        if PHONE_HOME_SDKS.search(line):
            hits.append(f"{_rel(gradle)}:{lineno}: {line.strip()}")
    assert not hits, (
        "an analytics or crash-reporting SDK is declared:\n  " + "\n  ".join(hits)
        + "\n\nThese send data off the device by design. Adding one is a product "
          "decision that contradicts the privacy claim on the front page, not a "
          "dependency bump.")


def test_the_webview_is_never_pointed_at_a_remote_page():
    """`server.url` makes Capacitor load the app FROM a URL.

    It is the one config line that turns a local-only app into a thin client for
    somebody's server, and it is a single line to add.
    """
    cfg = MOBILE / "capacitor.config.ts"
    if not cfg.exists():
        pytest.skip("no capacitor.config.ts")
    body = cfg.read_text(encoding="utf-8")
    live = [l for l in body.splitlines()
            if re.search(r"^\s*url\s*:", l) and not l.lstrip().startswith(("//", "*"))]
    assert not live, (
        f"capacitor.config.ts sets a server URL: {live}. The WebView must load "
        f"the bundled app, never a remote page.")


def test_cleartext_is_off_and_user_cas_are_not_trusted():
    """Two lines that quietly widen what the app will talk to."""
    manifest = MOBILE / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    nsc = (MOBILE / "android" / "app" / "src" / "main" / "res" / "xml"
           / "network_security_config.xml")
    if not manifest.exists():
        pytest.skip("no AndroidManifest.xml")

    m = manifest.read_text(encoding="utf-8")
    assert 'android:usesCleartextTraffic="false"' in m, (
        "the manifest no longer forbids cleartext traffic")

    if nsc.exists():
        # Comments stripped: this file DOCUMENTS, at length, which trust
        # widenings it deliberately does not use. Reading the explanation as the
        # thing it explains is how the first version of this test failed.
        n = _code_only(nsc.read_text(encoding="utf-8"), nsc.suffix)
        assert 'cleartextTrafficPermitted="true"' not in n, (
            "the network security config permits cleartext")
        assert 'src="user"' not in n, (
            "the network security config trusts user-installed CAs, so a locally "
            "added MitM certificate would be accepted")
        assert "<debug-overrides>" not in n, (
            "debug-overrides widen trust in debuggable builds, which is where "
            "side-loaded test APKs run")


def test_the_bridge_never_logs_plugin_arguments():
    """Capacitor's own bridge verbose-logs every plugin call's arguments, which
    includes the Authorization header passed into the native client. That is
    framework-side, before any plugin code runs, so plugin-side redaction cannot
    cover it."""
    cfg = MOBILE / "capacitor.config.ts"
    if not cfg.exists():
        pytest.skip("no capacitor.config.ts")
    assert re.search(r"loggingBehavior\s*:\s*'none'",
                     cfg.read_text(encoding="utf-8")), (
        "loggingBehavior is no longer 'none'; the bridge will log bearer tokens "
        "to logcat in debuggable builds")


def test_this_guard_can_actually_fail():
    """The control.

    Every assertion above is a negative — "nothing here names an external host" —
    and a scanner that reads nothing satisfies all of them. This plants each shape
    and asserts it is recognised.
    """
    assert URL.findall("fetch('https://telemetry.example.com/collect')")
    # Comment stripping blanks the comment and keeps the line count. Built
    # with an explicit newline rather than an escape: this control is ABOUT
    # newline handling, and an escaped literal is the one thing that should
    # not be in the middle of it.
    nl = chr(10)
    commented = nl.join(["a = 1", "// see https://telemetry.example.com", ""])
    live = nl.join(["fetch('https://telemetry.example.com')", ""])
    assert "telemetry" not in _code_only(commented, ".js")
    assert "telemetry" in _code_only(live, ".js")
    xml = nl.join(["x", "<!-- <debug-overrides> -->", "y", ""])
    assert _code_only(xml, ".xml").count(nl) == xml.count(nl)
    assert "debug-overrides" not in _code_only(xml, ".xml")
    assert PHONE_HOME_SDKS.search("implementation 'com.google.firebase:firebase-analytics'")
    assert not PHONE_HOME_SDKS.search("implementation 'com.squareup.okhttp3:okhttp:4.12.0'")
    assert _shipped_sources(), "the file walk found nothing, so it proves nothing"
