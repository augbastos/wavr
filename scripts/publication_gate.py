#!/usr/bin/env python3
"""Answer one question about this repository: is it safe to make public?

Run it from the repository root:

    python scripts/publication_gate.py

Exit code 0 means no blocker was found. Exit 1 means at least one was, and the
reason is printed. Stdlib only, no network beyond one `git ls-remote`, no paid
service, and it changes nothing — it reads the tree and reports.

WHY THIS EXISTS
---------------
Publication is a one-way door. A hard-coded home coordinate, an absolute path
with an account name in it, a vendored library with no licence: a private
repository forgives all of them, because the only reader already knows. The
instant the visibility flips they are published, indexed and mirrored, and
deleting the file afterwards does not unpublish it.

So the checks here are the ones where "we would have caught that in review" is
not good enough, because review is exactly what missed them the first time.

WHY IT NO LONGER KNOWS ANY SECRETS
----------------------------------
An earlier version of this file worked from a denylist of the exact private
strings that had been removed from the tree, and excluded itself from its own
scan so it would not report them.

That is incoherent for a file meant to be published: a privacy checker cannot be
the thing that publishes the secret. It was, in fact, the only file left in the
tree carrying any of those values, and the self-exclusion is precisely what hid
that from the tool itself.

So this file now knows only SHAPES:

  * a coordinate precise enough to be a doorstep, in a context that reads as a
    home;
  * an absolute path that belongs to one machine and names one account;
  * a private key, a keystore, a database, an environment file;
  * a credential-shaped literal;
  * a claim about the repository's own visibility.

A shape catches the NEXT mistake. A list of old strings only ever catches the
last one. If an exact-literal check is still wanted, keep it OUTSIDE this
repository:

    python scripts/publication_gate.py --denylist ~/private/wavr-denylist.txt

One pattern per line, `#` for comments, never read from inside the repository,
never required, and not used by contributor CI. Nothing private is committed so
that a public tool can look for it later.

SEVERITIES
----------
  BLOCKER        publishing with this present would disclose something. Fix first.
  STALE-ON-FLIP  true today, FALSE the moment the repository is public. Not a
                 defect now; an edit that must land in the same push as the
                 visibility change, or the documentation starts lying.
  NOTE           worth a human's eye, not worth blocking on.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

BLOCKER, STALE, NOTE = "BLOCKER", "STALE-ON-FLIP", "NOTE"


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
    return [p for p in out.splitlines() if p]


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, IsADirectoryError):
        return None


TEXT_SUFFIXES = (
    ".py", ".js", ".mjs", ".ts", ".kt", ".rs", ".java", ".md", ".txt", ".html",
    ".css", ".json", ".yml", ".yaml", ".toml", ".ps1", ".sh", ".cfg", ".ini",
    ".gradle", ".xml", ".properties", ".svg", ".bat",
)


def text_files(files: list[str]) -> list[str]:
    return [p for p in files if p.endswith(TEXT_SUFFIXES)]


# ---------------------------------------------------------------------------
# 1. Files that must never be tracked at all
# ---------------------------------------------------------------------------
# Not "should be gitignored" — ignoring an already-tracked file changes nothing.
# The question is whether the file is IN the index right now.

FORBIDDEN_TRACKED = [
    # `.env` itself, never `.env.example` / `.env.sample` / `.env.template`:
    # a template holding placeholder values is the file that tells a
    # contributor WHICH variables exist. Refusing it would push the same
    # list into prose that nobody keeps current.
    (re.compile(r"(^|/)\.env$|(^|/)\.env\.(?!example$|sample$|template$|dist$)"),
     "an environment file holds live credentials"),
    (re.compile(r"\.(db|sqlite3?)$"), "a database holds real observations"),
    (re.compile(r"\.(pem|key|p12|pfx|jks|keystore)$"), "a private key or keystore"),
    (re.compile(r"(^|/)local\.properties$"), "machine-local paths and signing config"),
    (re.compile(r"(^|/)keystore\.properties$"), "signing configuration"),
    (re.compile(r"(^|/)secrets?\.(json|ya?ml|toml)$"), "a secrets file"),
    (re.compile(r"(^|/)id_(rsa|ed25519|ecdsa)$"), "an SSH private key"),
]


def check_forbidden_files(files: list[str], rep: Report) -> None:
    for path in files:
        for pattern, why in FORBIDDEN_TRACKED:
            if pattern.search(path):
                rep.add(BLOCKER, "tracked-file", f"{path} — {why}")


# ---------------------------------------------------------------------------
# 2. Signing material is actually ignored, not merely believed to be
# ---------------------------------------------------------------------------
# The contributor documentation says keystores and machine-local property files
# are gitignored. That sentence was FALSE when it was first written: the Android
# Studio template ships its keystore lines COMMENTED OUT, and nothing covered the
# repository root, so most of these paths were unprotected while the docs said
# otherwise.
#
# The failure mode is not hypothetical and not recoverable. Somebody reads the
# sentence, trusts it, and a signing key lands in a repository about to go
# public. Whoever then holds that key can sign an update the platform accepts on
# every device that has the app. No rotation fixes a published key.

MUST_BE_IGNORED = [
    "release.keystore", "keystore.properties", "local.properties",
    "mobile/local.properties", "mobile/android/local.properties",
    "mobile/android/app/release.jks", "mobile/android/app/upload.keystore",
    "core-launcher/local.properties", "core-launcher/app/release.jks",
    "desktop/cert.p12", ".env",
]


def check_ignored(rep: Report) -> None:
    for path in MUST_BE_IGNORED:
        ignored = subprocess.run(["git", "check-ignore", "-q", path],
                                 capture_output=True).returncode == 0
        if not ignored:
            rep.add(BLOCKER, "gitignore",
                    f"{path} is NOT ignored. Signing material or a machine-local "
                    f"path can be committed by accident, and a leaked signing key "
                    f"cannot be un-leaked.")


# ---------------------------------------------------------------------------
# 3. Shapes that should never be published
# ---------------------------------------------------------------------------
# Generic, not remembered. Each describes a KIND of disclosure rather than a
# specific past one.

# A Windows drive path or a Unix home path pinned to one account. These carry an
# account name and a machine layout.
ABSOLUTE_PATH = re.compile(
    r"""(?xi)
    (?: [A-Z]:[\\/](?:Users|home)[\\/][A-Za-z0-9._-]+
      | (?<![\w/])/(?:home|Users)/[A-Za-z0-9._-]+
    )""")

# Six or more decimal places of latitude is roughly a doorstep; five is a house.
# Precision like that sitting next to a word like "home" is either a real address
# or a fixture that reads exactly like one — and a reader cannot tell which.
HOME_COORD = re.compile(
    r"(?i)(?:home|house|casa|residen)[a-z_]*\s*[=:]\s*['\"]?-?\d{1,3}\.\d{5,}")
COORD_PAIR_PRECISE = re.compile(r"-?\d{1,3}\.\d{6,}\s*,\s*-?\d{1,3}\.\d{6,}")

# Credential shapes. Deliberately narrow: a wide pattern produces false blockers,
# and a false blocker teaches people to ignore the tool.
CREDENTIALS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key id"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "a GitHub token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
     "a private key block"),
    (re.compile(r"(?i)\b(?:password|passwd|api[_-]?key)\s*[=:]\s*"
                r"['\"][^'\"\s{}$<>*]{10,}['\"]"), "a hard-coded credential"),
]

# An escape hatch that lives in the code being excused, not in a list inside
# this tool. A synthetic Windows path IS the fixture in a test that parses
# Windows firewall output, and no pattern can tell an invented account name
# from a real one. So the author asserts it, in the line, where a reviewer
# reading that line sees the assertion:
#
#     EXE = r"C:\\Users\\someone\\..."   # publication-gate: synthetic
#
# A list of exempt FILES would have hidden the same assertion somewhere
# nobody reading the fixture would look -- which is how the previous version
# of this gate came to be the only file in the tree still carrying real
# private values.
WAIVER = re.compile(r"publication-gate:\s*synthetic")

# Files that legitimately carry these shapes, or that this gate must not read as
# source: licence texts quote addresses, vendored bundles are minified noise.
SHAPE_EXEMPT = re.compile(
    r"(?:^|/)(?:LICENSE|LICENCE)[^/]*$|"
    r"(?:^|/)THIRD-PARTY-NOTICES\.md$|"
    r"(?:^|/)vendor/|(?:^|/)node_modules/|\.min\.(?:js|css)$")


def check_shapes(files: list[str], rep: Report, denylist: list[re.Pattern]) -> None:
    for path in text_files(files):
        if SHAPE_EXEMPT.search(path):
            continue
        body = read(path)
        if body is None:
            continue
        for lineno, line in enumerate(body.splitlines(), 1):
            if len(line) > 2000:      # a minified or generated line, not source
                continue
            if WAIVER.search(line):
                rep.add(NOTE, "waiver",
                        f"{path}:{lineno} asserts a synthetic value in-line. "
                        f"Read the line and confirm the assertion holds.")
                continue
            if ABSOLUTE_PATH.search(line):
                rep.add(BLOCKER, "absolute-path",
                        f"{path}:{lineno} contains an absolute path naming a local "
                        f"account. It identifies the machine and whoever runs it.")
            if HOME_COORD.search(line) or COORD_PAIR_PRECISE.search(line):
                rep.add(BLOCKER, "location",
                        f"{path}:{lineno} contains a coordinate precise enough to be "
                        f"a doorstep, in a context that reads as a home.")
            for pattern, what in CREDENTIALS:
                if pattern.search(line):
                    rep.add(BLOCKER, "credential",
                            f"{path}:{lineno} looks like {what}. Not printed here.")
            for pattern in denylist:
                if pattern.search(line):
                    rep.add(BLOCKER, "denylist",
                            f"{path}:{lineno} matches a maintainer denylist entry. "
                            f"Neither the pattern nor the match is printed.")


# ---------------------------------------------------------------------------
# 4. Vendored third-party code carries its licence
# ---------------------------------------------------------------------------
# A vendored file ships inside the product. With no licence, the product is
# redistributing somebody's code without the notice their licence requires —
# a licensing defect, not a documentation one.

def check_vendored(files: list[str], rep: Report) -> None:
    vendored = [p for p in files
                if re.search(r"(^|/)vendor(/|$)|\.vendor\.js$", p)
                and p.endswith((".js", ".mjs", ".css"))]
    if not vendored:
        rep.add(NOTE, "vendored", "no vendored assets found — is that right?")
    for path in vendored:
        head = (read(path) or "")[:4000]
        if "SPDX-License-Identifier:" not in head:
            rep.add(BLOCKER, "vendored",
                    f"{path} has no SPDX-License-Identifier in its first 4KB")
        if not re.search(r"[Cc]opyright|@license", head):
            rep.add(BLOCKER, "vendored",
                    f"{path} carries no copyright or @license line")

    notices = read("THIRD-PARTY-NOTICES.md")
    if notices is None:
        rep.add(BLOCKER, "vendored", "THIRD-PARTY-NOTICES.md is missing")
        return
    for ref in re.findall(r"`([^`]*LICENSE[^`]*)`", notices):
        # Only path-shaped references: prose names a licence file by its bare name
        # too, and treating those as paths produced a false blocker once.
        if "/" not in ref:
            continue
        if not os.path.exists(ref):
            rep.add(BLOCKER, "vendored",
                    f"THIRD-PARTY-NOTICES.md points at {ref}, which does not exist")


# ---------------------------------------------------------------------------
# 5. The licence is declared everywhere a reader looks
# ---------------------------------------------------------------------------

LICENCE = "AGPL-3.0-or-later"
DECLARATIONS = ["backend/pyproject.toml", "desktop/package.json",
                "desktop/src-tauri/Cargo.toml"]


def check_licence(rep: Report) -> None:
    if not os.path.exists("LICENSE"):
        rep.add(BLOCKER, "licence", "there is no LICENSE file")
    for path in DECLARATIONS:
        body = read(path)
        if body is None:
            rep.add(NOTE, "licence", f"{path} not found — skipped")
        elif LICENCE not in body:
            rep.add(BLOCKER, "licence", f"{path} does not declare {LICENCE}")

    readme = read("README.md") or ""
    if "AGPL--3.0--or--later" not in readme and LICENCE not in readme:
        rep.add(BLOCKER, "licence",
                "README.md does not state AGPL-3.0-or-later; a badge reading plain "
                "'AGPL-3.0' claims a narrower licence than LICENSE grants")


# ---------------------------------------------------------------------------
# 6. Workflows declare their token permissions
# ---------------------------------------------------------------------------
# On a public repository anybody can open a pull request, and a
# `pull_request`-triggered workflow runs with whatever the account's default
# token grant happens to be — a property no reader of this repository can see.

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
# 7. The branch every published URL names actually exists
# ---------------------------------------------------------------------------
# `docs/INSTALL.md` hands a stranger a one-line installer that fetches a raw URL
# pinned to a branch name, and the site links documents the same way. If that
# branch does not exist, every one of them is a 404 on the day of publication —
# and nothing in the tree fails, because the tree does not know what the remote
# is called.

# `api.github.com/repos/<owner>/<repo>/...` carries an extra path segment,
# so this same pattern reads the REPO name as a branch. Excluded by a
# lookbehind rather than a special case further down, because the API host
# is a genuinely different URL shape and not an exception to this one.
BRANCH_IN_URLS = re.compile(
    r"(?<!api\.)github(?:usercontent)?\.com/[\w-]+/[\w.-]+/"
    r"(?:blob/|tree/|raw/)?([\w.-]+)/")

# `<owner>/<repo>/<segment>/` is not always a branch: badges go through /actions/,
# and issues, releases and the rest look identical to a regex. Listing them beats
# a cleverer pattern — a wrong "branch missing" blocker teaches people to ignore
# this tool, which costs more than the check is worth.
NOT_A_BRANCH = frozenset({
    "actions", "issues", "pull", "pulls", "releases", "commit", "commits",
    "compare", "wiki", "security", "settings", "graphs", "network", "archive",
    "labels", "milestones", "projects", "discussions", "assets", "raw", "blob",
    "tree",
})


def check_url_branch(files: list[str], rep: Report) -> None:
    named: dict[str, str] = {}
    for path in text_files(files):
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
                    f"which does not exist on the remote. Every such link 404s, "
                    f"including the one-line installer. Remote has: "
                    f"{', '.join(sorted(real))}")


# ---------------------------------------------------------------------------
# 8. Publication-critical documents exist
# ---------------------------------------------------------------------------

REQUIRED_DOCS = ["README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
                 "THIRD-PARTY-NOTICES.md", "AGENTS.md", "PRODUCT.md"]


def check_required_docs(rep: Report) -> None:
    for path in REQUIRED_DOCS:
        if not os.path.exists(path):
            rep.add(BLOCKER, "docs", f"{path} is missing")


# ---------------------------------------------------------------------------
# 9. Statements that are true now and false the moment this is public
# ---------------------------------------------------------------------------
# These are not defects. They are the price of documenting the current state
# honestly, and the repository is better for having them. But each one has to
# change in the SAME push as the visibility flip, or the documentation begins
# lying on day one — which is worse than never having said anything.
#
# Each anchor is also a tripwire in the other direction: if it stops matching,
# somebody edited the passage and this list needs re-reading.

VISIBILITY_CLAIMS = [
    (".github/workflows/tests.yml",
     "since this repository is private, where",
     "runner minutes stop coming out of the account allowance — a public "
     "repository's Actions minutes are free. The cancellation is still right; the "
     "reason changes."),
    (".github/workflows/tests.yml",
     "This repository is private, so its runner minutes",
     "same as above: the cost argument for the `what-changed` gate weakens, though "
     "the honesty argument for it does not."),
    (".github/workflows/docker.yml",
     "on a private repository the minutes as well",
     "minutes are no longer the reason; drop the clause."),
    ("CONTRIBUTING.md",
     "repository is private. That is stated here",
     "THIS ONE INVERTS. Rulesets are refused on a private repository on the free "
     "plan and DO work on a public one, so publication is what makes branch "
     "protection possible. Enable a ruleset on the default branch in the same "
     "session, and rewrite this passage to say what it protects."),
    ("CONTRIBUTING.md",
     "on a private repository those minutes come out",
     "the browser job is free once public; keep the gate, change the reason."),
    ("project.json",
     '"visibility": "private',
     "the machine-readable map states the visibility. Change it. This file says of "
     "itself that a stale map is worse than none."),
    ("docs/INSTALL.md",
     "raw.githubusercontent.com",
     "THIS ONE BECOMES TRUE. A raw.githubusercontent URL 404s on a private "
     "repository, so the documented one-line installer cannot work today and starts "
     "working on publication. Run it once, for real, immediately after."),
    ("PRODUCT.md",
     "public repo",
     "THIS ONE BECOMES TRUE. It describes a reader judging the work from the public "
     "repository — nobody outside can do that today."),
    # The site is a publication surface too, and the first version of this list
    # contained none of it. Five passages there say a file is "not on the public
    # repository yet" -- true today, false the moment it is, with nothing to
    # notice.
    ("site/public/install.html",
     "not on the public repository yet",
     "four passages say a file is not on the public repository yet. It will be. "
     "Replace each with a direct link to the file."),
    ("site/public/docs.html",
     "Not on the public repository yet",
     "same, once."),
    ("SECURITY.md",
     "enable endpoint refuses it",
     "private vulnerability reporting becomes available. ENABLE IT, then delete the "
     "status block — it describes a channel that now exists."),
]


def check_visibility_claims(rep: Report) -> None:
    for path, anchor, todo in VISIBILITY_CLAIMS:
        body = read(path)
        if body is None:
            rep.add(NOTE, "visibility", f"{path} not found — was it renamed?")
        elif anchor not in body:
            rep.add(NOTE, "visibility",
                    f"{path}: an anchor no longer matches. Either it was already "
                    f"fixed, or the passage moved and this list is stale.")
        else:
            rep.add(STALE, "visibility", f"{path} — {todo}")


# ---------------------------------------------------------------------------

CHECKS = [
    ("forbidden files", lambda f, r, d: check_forbidden_files(f, r)),
    ("signing material ignored", lambda f, r, d: check_ignored(r)),
    ("disclosure shapes", lambda f, r, d: check_shapes(f, r, d)),
    ("vendored licences", lambda f, r, d: check_vendored(f, r)),
    ("licence declared", lambda f, r, d: check_licence(r)),
    ("workflow permissions", lambda f, r, d: check_workflow_permissions(r)),
    ("URL branch exists", lambda f, r, d: check_url_branch(f, r)),
    ("publication docs", lambda f, r, d: check_required_docs(r)),
    ("visibility claims", lambda f, r, d: check_visibility_claims(r)),
]


def load_denylist(path: str | None, rep: Report) -> list[re.Pattern]:
    """Optional, maintainer-only, and never from inside this repository.

    Nothing private is committed so that a public tool can look for it later. If
    the file is absent the gate still runs — this is an extra, not a dependency,
    and contributor CI does not use it.
    """
    if not path:
        return []
    resolved = os.path.abspath(os.path.expanduser(path))
    if resolved.startswith(os.path.abspath(os.curdir) + os.sep):
        rep.add(BLOCKER, "denylist",
                "the denylist is inside the repository. That is the exact mistake "
                "this option exists to avoid — it would publish the values it "
                "searches for. Move it outside and pass an absolute path.")
        return []
    try:
        with open(resolved, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh]
    except OSError as exc:
        rep.add(NOTE, "denylist", f"could not read the maintainer denylist: {exc}")
        return []
    out = []
    for line in lines:
        if not line or line.startswith("#"):
            continue
        try:
            out.append(re.compile(line))
        except re.error:
            out.append(re.compile(re.escape(line)))
    rep.add(NOTE, "denylist",
            f"{len(out)} maintainer pattern(s) loaded from outside the repository")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Is this repository safe to make public?")
    ap.add_argument("--denylist", metavar="PATH",
                    default=os.environ.get("WAVR_PRIVATE_DENYLIST"),
                    help="optional maintainer-only pattern file, OUTSIDE this "
                         "repository (or set WAVR_PRIVATE_DENYLIST)")
    args = ap.parse_args()

    if not os.path.isdir(".git"):
        print("run this from the repository root", file=sys.stderr)
        return 2

    files = tracked_files()
    rep = Report()
    denylist = load_denylist(args.denylist, rep)
    for name, fn in CHECKS:
        fn(files, rep, denylist)
        print(f"  ran: {name}")

    print(f"\nscanned {len(files)} tracked files\n")

    for severity in (BLOCKER, STALE, NOTE):
        rows = [(c, d) for s, c, d in rep.findings if s == severity]
        if not rows:
            continue
        header = f"{severity} ({len(rows)})"
        print(header)
        print("-" * len(header))
        for check, detail in rows:
            print(f"  [{check}] {detail}")
        print()

    blockers = rep.count(BLOCKER)
    if blockers:
        print(f"NOT READY — {blockers} blocker(s) above must be resolved first.")
        return 1

    stale = rep.count(STALE)
    print("No blockers.")
    if stale:
        print(f"{stale} passage(s) become false when this repository goes public. "
              f"They are listed above and must be edited in the same push as the "
              f"visibility change.")
    print("\nThis gate proves the absence of the mistakes it knows the SHAPE of, "
          "not the absence of mistakes. It is one input to the decision, never the "
          "decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
