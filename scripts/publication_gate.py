#!/usr/bin/env python3
"""Answer one question about the working tree: is it safe to make this repository public?

Run it from the repository root:

    python scripts/publication_gate.py

Exit code 0 means no blocker was found. Exit code 1 means at least one was, and
the reason is printed. Nothing here touches the network, calls a paid service, or
changes a file -- it reads the tree and reports.

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
Publication is a one-way door. A private repository forgives a hard-coded home
coordinate, an absolute path with a username in it, or a vendored library with no
licence, because the only reader already knows all of it. The instant the
visibility flips, every one of those is published, indexed and mirrored, and
deleting the file afterwards does not unpublish it.

So the checks below are the ones where "we would have caught that in review" is
not good enough, because review is exactly what missed them the first time.

This is NOT a secret scanner. It knows a small set of literal strings that were
found in this repository once and must never come back. A real scan of the full
reachable history is a separate job, and the two are not substitutes: this one
answers "did a known mistake return", not "is there something new".

Three severities:

  BLOCKER        publishing with this present would disclose something. Fix first.
  STALE-ON-FLIP  true today, FALSE the moment the repository is public. These are
                 not defects now; they are edits that must land in the same push
                 as the visibility change, or the documentation starts lying.
  NOTE           worth a human's eye, not worth blocking on.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

BLOCKER, STALE, NOTE = "BLOCKER", "STALE-ON-FLIP", "NOTE"

# This file names the very strings it forbids, so it must never scan itself.
SELF = "scripts/publication_gate.py"


@dataclass
class Report:
    findings: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, severity: str, check: str, detail: str) -> None:
        self.findings.append((severity, check, detail))

    def count(self, severity: str) -> int:
        return sum(1 for s, _, _ in self.findings if s == severity)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                         check=True).stdout
    return [p for p in out.splitlines() if p and p != SELF]


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, IsADirectoryError):
        return None


# ---------------------------------------------------------------------------
# 1. Files that must never be tracked at all
# ---------------------------------------------------------------------------
# Not "should be gitignored" -- ignoring an already-tracked file changes nothing.
# The question is whether the file is IN the index right now.

FORBIDDEN_TRACKED = [
    (re.compile(r"(^|/)\.env$"), "an environment file holds live credentials"),
    (re.compile(r"\.(db|sqlite3?)$"), "a database holds real presence history"),
    (re.compile(r"\.(pem|key|p12|pfx|jks|keystore)$"), "a private key or keystore"),
    (re.compile(r"(^|/)local\.properties$"), "Android local paths and signing config"),
    (re.compile(r"(^|/)secrets?\.(json|ya?ml|toml)$"), "a secrets file"),
]


def check_forbidden_files(files: list[str], rep: Report) -> None:
    for path in files:
        for pattern, why in FORBIDDEN_TRACKED:
            if pattern.search(path):
                rep.add(BLOCKER, "tracked-file", f"{path} -- {why}")


# ---------------------------------------------------------------------------
# 2. Strings that were removed once and must not come back
# ---------------------------------------------------------------------------
# Each of these was found in this tree during the pre-publication audit. A
# regression is a real possibility: a revert, a cherry-pick from an old branch,
# a file restored from a worktree. The point of pinning them is that the second
# occurrence is caught by a command instead of by luck.

REGRESSIONS = [
    # Six decimal places of latitude is about ten centimetres. The variable was
    # called WAVR_HOME_LAT.
    ("40.689247", "the maintainer's home latitude, to ~10cm"),
    ("74.044502", "the maintainer's home longitude, to ~10cm"),
    ("40.6892", "the same coordinate at lower precision"),
    ("74.0445", "the same coordinate at lower precision"),
    # An absolute path carries the account name of whoever ran the command.
    ("owner", "a Windows account name from an absolute path"),
    (r"<local>", "a machine-specific absolute path"),
    ("<local>", "a machine-specific absolute path"),
    # Hardware identity: the machine, and the handsets on the household network.
    ("the GPU", "the maintainer's GPU model"),
    ("a laptop", "the maintainer's laptop model"),
    ("a commercial VPN", "the maintainer's VPN provider"),
    ("a handset", "a handset model on the maintainer's network"),
    ("the field device", "a handset model on the maintainer's network"),
    ("lan.gateway", "the maintainer's ISP router domain"),
]

TEXT_SUFFIXES = (
    ".py", ".js", ".mjs", ".ts", ".kt", ".rs", ".java", ".md", ".txt", ".html",
    ".css", ".json", ".yml", ".yaml", ".toml", ".ps1", ".sh", ".cfg", ".ini",
    ".gradle", ".xml", ".properties", ".d.ts", ".svg",
)


def check_regressions(files: list[str], rep: Report) -> None:
    for path in files:
        if not path.endswith(TEXT_SUFFIXES):
            continue
        body = read(path)
        if body is None:
            continue
        for needle, why in REGRESSIONS:
            if needle in body:
                line = next((i for i, l in enumerate(body.splitlines(), 1)
                             if needle in l), 0)
                rep.add(BLOCKER, "regression",
                        f"{path}:{line} contains {needle!r} -- {why}")


# ---------------------------------------------------------------------------
# 2b. Signing material is actually ignored, not just believed to be
# ---------------------------------------------------------------------------
# AGENTS.md tells every contributor and every agent that keystores and
# `local.properties` are gitignored. That sentence was FALSE when it was
# written: `mobile/android/.gitignore` ships the Android Studio template, which
# comments its keystore lines out, and nothing covered the repository root at
# all. Five of seven paths were unprotected.
#
# The failure mode is not hypothetical and not recoverable. Somebody reads the
# sentence, trusts it, and an APK signing key lands in a repository about to go
# public. Whoever then holds that key can sign an update Android accepts on
# every device that has the app. There is no rotation that fixes an already
# published key.
#
# So the claim gets a producer: these paths are asked of git itself.

MUST_BE_IGNORED = [
    "release.keystore",
    "keystore.properties",
    "local.properties",
    "mobile/local.properties",
    "mobile/android/local.properties",
    "mobile/android/app/release.jks",
    "mobile/android/app/upload.keystore",
    "core-launcher/local.properties",
    "core-launcher/app/release.jks",
    "desktop/cert.p12",
]


def check_ignored(rep: Report) -> None:
    for path in MUST_BE_IGNORED:
        ignored = subprocess.run(["git", "check-ignore", "-q", path]).returncode == 0
        if not ignored:
            rep.add(BLOCKER, "gitignore",
                    f"{path} is NOT ignored. A signing key or a machine-local "
                    f"path can be committed by accident, and a leaked signing "
                    f"key cannot be un-leaked.")


# ---------------------------------------------------------------------------
# 2c. The branch every published URL names actually exists
# ---------------------------------------------------------------------------
# `docs/INSTALL.md` hands a stranger a one-line installer that fetches a raw
# URL pinned to a branch name. `site/public/*.html` links three dozen documents
# the same way. If that branch does not exist, every one of them is a 404 the
# day the repository goes public -- and nothing in the tree fails, because the
# tree has no idea what the remote is called.

BRANCH_IN_URLS = re.compile(r"github(?:usercontent)?\.com/[\w-]+/wavr/"
                            r"(?:blob/|tree/|raw/)?([\w.-]+)/")

# github.com/<owner>/wavr/<segment>/ is not always a branch: the badge URLs go
# through /actions/, and issues, releases and the rest look identical to a regex.
# Listing them beats a cleverer pattern -- a wrong "branch missing" blocker
# teaches people to ignore this tool, which costs more than the check is worth.
NOT_A_BRANCH = frozenset({
    "actions", "issues", "pull", "pulls", "releases", "commit", "commits",
    "compare", "wiki", "security", "settings", "graphs", "network", "archive",
    "labels", "milestones", "projects", "discussions", "assets", "raw", "blob",
    "tree",
})


def check_url_branch(files: list[str], rep: Report) -> None:
    named: dict[str, str] = {}
    for path in files:
        if not path.endswith((".md", ".html", ".ps1", ".sh", ".toml", ".json")):
            continue
        for branch in set(BRANCH_IN_URLS.findall(read(path) or "")):
            if branch in NOT_A_BRANCH:
                continue
            named.setdefault(branch, path)
    if not named:
        return
    out = subprocess.run(["git", "ls-remote", "--heads", "origin"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        rep.add(NOTE, "url-branch",
                "could not reach the remote to check the branches these URLs name")
        return
    real = {line.split("refs/heads/")[-1] for line in out.stdout.splitlines()
            if "refs/heads/" in line}
    for branch, where in sorted(named.items()):
        if branch not in real:
            rep.add(BLOCKER, "url-branch",
                    f"published URLs name branch {branch!r} (first seen in {where}), "
                    f"which does not exist on origin. Every such link 404s, "
                    f"including the one-line installer. Remote has: "
                    f"{', '.join(sorted(real))}")


# ---------------------------------------------------------------------------
# 3. Vendored third-party code carries its licence
# ---------------------------------------------------------------------------
# A vendored file ships inside the product. If it carries no licence, the
# product is redistributing somebody's code without the notice their licence
# requires -- which is a licensing defect, not a documentation one.

def check_vendored(files: list[str], rep: Report) -> None:
    vendored = [p for p in files
                if re.search(r"(^|/)vendor(/|$)|\.vendor\.js$", p)
                and p.endswith((".js", ".mjs", ".css"))]
    if not vendored:
        rep.add(NOTE, "vendored", "no vendored assets found -- is that right?")
    for path in vendored:
        body = read(path) or ""
        head = body[:4000]
        if "SPDX-License-Identifier:" not in head:
            rep.add(BLOCKER, "vendored",
                    f"{path} has no SPDX-License-Identifier in its first 4KB")
        if not re.search(r"[Cc]opyright|@license", head):
            rep.add(BLOCKER, "vendored", f"{path} carries no copyright line")

    notices = read("THIRD-PARTY-NOTICES.md")
    if notices is None:
        rep.add(BLOCKER, "vendored", "THIRD-PARTY-NOTICES.md is missing")
        return
    # Every licence file the notices point at must actually exist.
    for ref in re.findall(r"`([^`]*LICENSE[^`]*)`", notices):
        # Only path-shaped references. Prose mentions a licence file by its bare
        # name too ("`LICENSE-jsQR` was added"), and treating those as paths made
        # this check report a missing file that was sitting right where the table
        # said it was -- a false blocker, which is the one kind of finding that
        # teaches people to ignore the tool.
        if "/" not in ref:
            continue
        if not os.path.exists(ref):
            rep.add(BLOCKER, "vendored",
                    f"THIRD-PARTY-NOTICES.md points at {ref}, which does not exist")


# ---------------------------------------------------------------------------
# 4. The licence is declared everywhere a reader looks
# ---------------------------------------------------------------------------

LICENCE = "AGPL-3.0-or-later"

DECLARATIONS = [
    ("backend/pyproject.toml", LICENCE),
    ("desktop/package.json", LICENCE),
    ("desktop/src-tauri/Cargo.toml", LICENCE),
]


def check_licence(rep: Report) -> None:
    if not os.path.exists("LICENSE"):
        rep.add(BLOCKER, "licence", "there is no LICENSE file")
    for path, expected in DECLARATIONS:
        body = read(path)
        if body is None:
            rep.add(NOTE, "licence", f"{path} not found -- skipped")
        elif expected not in body:
            rep.add(BLOCKER, "licence", f"{path} does not declare {expected}")

    readme = read("README.md") or ""
    if "AGPL--3.0--or--later" not in readme and "AGPL-3.0-or-later" not in readme:
        rep.add(BLOCKER, "licence",
                "README.md does not state AGPL-3.0-or-later; a badge that says "
                "plain 'AGPL-3.0' claims a narrower licence than LICENSE grants")


# ---------------------------------------------------------------------------
# 5. Workflows declare their token permissions
# ---------------------------------------------------------------------------
# On a public repository, anybody can open a pull request, and a
# `pull_request`-triggered workflow runs with whatever the account's default
# token grant happens to be. A workflow that does not say what it needs is
# depending on an account setting no reader of this repository can see.

def check_workflow_permissions(rep: Report) -> None:
    root = os.path.join(".github", "workflows")
    if not os.path.isdir(root):
        rep.add(NOTE, "workflows", "no .github/workflows directory")
        return
    for name in sorted(os.listdir(root)):
        if not name.endswith((".yml", ".yaml")):
            continue
        body = read(os.path.join(root, name)) or ""
        if not re.search(r"^permissions:", body, re.M):
            rep.add(BLOCKER, "workflows",
                    f"{name} declares no top-level `permissions:`")


# ---------------------------------------------------------------------------
# 6. Statements that are true now and false the moment the repository is public
# ---------------------------------------------------------------------------
# These are not defects. They are the price of documenting the current state
# honestly, and the repository is better for having them. But every one of them
# has to change in the SAME push as the visibility flip, or the documentation
# begins lying on day one -- which is worse than never having said anything.
#
# Each anchor is also a tripwire in the other direction: if it stops matching,
# somebody edited the passage and this list needs re-reading.

VISIBILITY_CLAIMS = [
    (".github/workflows/tests.yml",
     "since this repository is private, where",
     "runner minutes stop coming out of the allowance -- a public repository's "
     "Actions minutes are free. The cancellation is still right; the reason changes."),
    (".github/workflows/tests.yml",
     "This repository is private, so its runner minutes",
     "same as above: the cost argument for the `what-changed` gate weakens, "
     "though the honesty argument for it does not."),
    (".github/workflows/docker.yml",
     "on a private repository the minutes as well",
     "minutes are no longer the reason; drop the clause."),
    ("CONTRIBUTING.md",
     "repository is private. That is stated here",
     "THIS ONE INVERTS. Rulesets are refused on a private repository on the free "
     "plan, but they DO work on a public one. So publication is what makes branch "
     "protection possible -- enable a ruleset on `main` in the same session, and "
     "rewrite this passage to say what it protects instead of why it cannot."),
    ("CONTRIBUTING.md",
     "on a private repository those minutes come out",
     "the eleven minutes are free once public; keep the gate, change the reason."),
    ("project.json",
     '"visibility": "private',
     "the machine-readable map states the visibility. Change it to \"public\"; a "
     "stale map is worse than none, and this file says so about itself."),
    ("docs/INSTALL.md",
     "raw.githubusercontent.com/augbastos/wavr/main/scripts/install.ps1",
     "THIS ONE BECOMES TRUE. A raw.githubusercontent URL 404s on a private "
     "repository, so the documented one-line installer cannot work today and "
     "starts working on publication. Run it once, for real, immediately after "
     "the flip -- a documented install command nobody has executed is a claim."),
    ("SECURITY.md",
     "GitHub's own enable endpoint refuses it",
     "private vulnerability reporting becomes available. ENABLE IT, then delete "
     "the whole 'Status, stated rather than assumed' block -- it describes a "
     "channel that now exists."),
]


def check_visibility_claims(rep: Report) -> None:
    for path, anchor, todo in VISIBILITY_CLAIMS:
        body = read(path)
        if body is None:
            rep.add(NOTE, "visibility", f"{path} not found -- was it renamed?")
        elif anchor not in body:
            rep.add(NOTE, "visibility",
                    f"{path}: anchor {anchor!r} no longer matches. Either it was "
                    f"already fixed, or the passage moved and this list is stale.")
        else:
            rep.add(STALE, "visibility", f"{path} -- {todo}")


# ---------------------------------------------------------------------------

CHECKS = [
    ("forbidden files", lambda files, rep: check_forbidden_files(files, rep)),
    ("known regressions", lambda files, rep: check_regressions(files, rep)),
    ("signing material ignored", lambda files, rep: check_ignored(rep)),
    ("URL branch exists", lambda files, rep: check_url_branch(files, rep)),
    ("vendored licences", lambda files, rep: check_vendored(files, rep)),
    ("licence declared", lambda files, rep: check_licence(rep)),
    ("workflow permissions", lambda files, rep: check_workflow_permissions(rep)),
    ("visibility claims", lambda files, rep: check_visibility_claims(rep)),
]


def main() -> int:
    if not os.path.isdir(".git"):
        print("run this from the repository root", file=sys.stderr)
        return 2

    files = tracked_files()
    rep = Report()
    for name, fn in CHECKS:
        fn(files, rep)
        print(f"  ran: {name}")

    print(f"\nscanned {len(files)} tracked files\n")

    for severity in (BLOCKER, STALE, NOTE):
        rows = [(c, d) for s, c, d in rep.findings if s == severity]
        if not rows:
            continue
        print(f"{severity} ({len(rows)})")
        print("-" * len(f"{severity} ({len(rows)})"))
        for check, detail in rows:
            print(f"  [{check}] {detail}")
        print()

    blockers = rep.count(BLOCKER)
    if blockers:
        print(f"NOT READY -- {blockers} blocker(s) above must be resolved first.")
        return 1

    stale = rep.count(STALE)
    print("No blockers.")
    if stale:
        print(f"{stale} passage(s) become false when the repository goes public. "
              f"They are listed above and must be edited in the same push as the "
              f"visibility change.")
    print("\nThis gate proves the absence of KNOWN mistakes, not the absence of "
          "mistakes. It is one input to the decision, not the decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
