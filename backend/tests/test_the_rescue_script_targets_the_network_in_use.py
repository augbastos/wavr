"""The recovery script must open the network the machine is actually on.

`_local/first-user-rc/liberar-wavr-na-minha-rede.ps1` is what the owner runs
when the phone cannot find the Core. It is the last clue he has: if it prints
"pronto" without having changed anything, it does not merely fail, it closes
off the investigation. So its failure mode matters more than most code's.

It failed exactly that way once, on 2026-09-06, and the way it failed is worth
writing down because the shape recurs.

Measured on this laptop: `Get-NetConnectionProfile` reported the home Wi-Fi as
**Public**. The script created its firewall rule with a hard-coded
`-Profile Private`, so the rule did not apply to the network in use. It then
printed success.

The first fix detected the live profile — and still got it wrong, because of a
second trap sitting underneath. `NetworkCategory.Public` is **0** in the enum,
and in PowerShell an array holding a single 0 is falsy, so the guard

    if (-not $perfis -or $perfis.Count -eq 0) { $perfis = @('Private') }

fired precisely when the detection had succeeded and the answer was `Public` —
overwriting the right answer with the wrong one, in the only case where the
script is needed at all. A guard against "I found nothing" that trips on a
valid finding is worse than no guard: it converts a correct result into a
silent error, visible only on the user's device.

## What these tests can and cannot see

Reading the text catches a hard-coded profile coming back, and catches the
falsy-enum shape returning in any form. That is worth having on every machine.

But text cannot tell you whether the value the script computes equals the value
Windows reports — and that equality is the whole claim. So on Windows, with
PowerShell available, the last test lifts the real block out of the real file,
runs it, and compares. It reads the block from the shipped script rather than
restating it here, because the first version of that check kept its own copy,
the copy drifted, and the copy answered correctly while the file answered
wrongly. One producer: a test that paraphrases what it is testing eventually
tests the paraphrase.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RC = REPO / "_local" / "first-user-rc"
RESCUE = RC / "liberar-wavr-na-minha-rede.ps1"
LAUNCH = REPO / "scripts" / "start_first_user_test.ps1"

pytestmark = pytest.mark.skipif(
    not RC.is_dir(),
    reason="the first-user release candidate is not in this checkout")


def source() -> str:
    return RESCUE.read_text(encoding="utf-8-sig", errors="replace")


def code_only(src: str) -> str:
    """The script minus its comments.

    The first version of the idiom check below scanned the raw text and flagged
    the comment that warns against the idiom -- so the file was penalised for
    explaining the trap it had just been fixed for, and the way to make the
    test pass was to delete the warning. A check that cannot tell using a thing
    from naming it will always push somebody to delete the naming.

    Quote handling is deliberate rather than clever: PowerShell has no escape
    for `'` other than doubling it, and `#` inside a double-quoted string is
    ordinary text, so tracking which quote (if any) is open is enough here.
    """
    out = []
    for line in src.splitlines():
        quote = ""
        cut = None
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "#":
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def test_there_is_a_rescue_script_to_check():
    """A guard on the guards: every assertion below reads this file."""
    assert RESCUE.is_file(), (
        f"{RESCUE} is missing — the owner has no recovery path, and the checks "
        f"below would all pass vacuously")
    assert source().strip(), "the rescue script is empty"


# -- The rule has to land on the network in use ------------------------------

def test_the_firewall_rule_does_not_hard_code_a_profile():
    """`-Profile Private` was right on the developer's assumptions and wrong on
    the developer's own laptop."""
    hard_coded = re.findall(
        r"-Profile\s+(Private|Public|Domain|Any|DomainAuthenticated)\b",
        code_only(source()))
    assert not hard_coded, (
        f"the rule is created with a fixed -Profile {hard_coded}. The Windows "
        f"network category is a property of the network the person happens to "
        f"be on, not of the product: a fixed profile is right on some networks "
        f"and silently inert on the rest.")


def test_the_profile_comes_from_the_machine():
    src = code_only(source())
    assert "Get-NetConnectionProfile" in src, (
        "nothing asks Windows which network this machine is on, so the rule "
        "cannot be aimed at it")
    assert re.search(r"-Profile\s+\$", src), (
        "the -Profile argument is not a variable, so whatever the detection "
        "found is not what the rule uses")


def test_the_rule_is_never_opened_on_every_profile():
    """The narrow scope is the reason this is safe to run at all: one program,
    inbound only, on the network in use. `Any` would also open networks the
    owner is not on and will not think about again."""
    assert not re.search(r"-Profile\s+['\"]?Any\b", code_only(source()), re.I), (
        "the rule would apply to every network profile, including ones the "
        "owner is not on")


# -- The falsy-enum trap, in any disguise ------------------------------------

def test_emptiness_is_tested_by_counting_not_by_truthiness():
    """`-not $perfis` is true when the one profile found is Public (enum 0).

    Written as a shape check rather than a check for the one line that broke,
    because the trap is the idiom, not the line. Comments are stripped first:
    see `code_only`.
    """
    offenders = [
        line.strip()
        for line in code_only(source()).splitlines()
        if re.search(r"-not\s+\$perfis\b", line)
        or re.search(r"if\s*\(\s*\$perfis\s*\)", line)
    ]
    assert not offenders, (
        f"emptiness is tested by truthiness: {offenders}. NetworkCategory."
        f"Public is 0, and a single-element array holding 0 is falsy, so this "
        f"fires when the detection SUCCEEDED and the answer was Public - the "
        f"one network where this script matters. Count instead.")


def test_the_category_is_turned_into_text_before_it_travels():
    """An enum that keeps its type keeps its zero, and the zero is the bug.

    Anywhere it is compared, joined or tested downstream, it has to already be
    a string.
    """
    src = code_only(source())
    assert not re.search(
        r"Select-Object\s+-ExpandProperty\s+NetworkCategory", src), (
        "NetworkCategory is expanded as an enum. Convert it with "
        '`ForEach-Object { "$($_.NetworkCategory)" }` so nothing downstream '
        "inherits a value that is numerically zero and therefore falsy.")
    assert re.search(r'"\$\(\$_\.NetworkCategory\)"', src), (
        "nothing converts NetworkCategory to text on the way out of the "
        "detection")


def test_the_final_check_compares_against_the_network_in_use():
    """The verification at the end used to compare the created rule against the
    literal `Private`. On a Public network it would announce a problem that was
    not one, and - worse, once the rule became dynamic - stay quiet about the
    one that was."""
    src = code_only(source())
    tail = src[src.find("$agora"):] if "$agora" in src else ""
    assert tail, "there is no final verification block to check"
    assert not re.search(r"-notmatch\s+'Private'", tail), (
        "the final check still compares the created rule against a hard-coded "
        "'Private' instead of the profile the machine is actually on")
    assert "$perfis" in tail, (
        "the final check does not reference the detected profiles, so it "
        "cannot notice that the rule missed them")


# -- Two traps this machine has already been bitten by -----------------------

@pytest.mark.parametrize("script", [RESCUE, LAUNCH], ids=lambda p: p.name)
def test_the_script_survives_this_machine(script):
    """A `.ps1` with a non-ASCII character and no BOM is misread by Windows
    PowerShell — an em dash in a rule name has broken a script on this machine
    before. Both conditions are cheap to hold, so hold both."""
    if not script.is_file():
        pytest.skip(f"{script.name} is not in this checkout")
    raw = script.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", (
        f"{script.name} has no UTF-8 BOM. Windows PowerShell then decodes it "
        f"as the ANSI code page, and one accented character is enough to turn "
        f"a working script into a parse error on the owner's machine.")
    text = raw[3:].decode("utf-8", errors="replace")
    non_ascii = sorted({c for c in text if ord(c) > 127})
    assert not non_ascii, (
        f"{script.name} contains non-ASCII characters {non_ascii}. The BOM "
        f"above should make them safe, but this machine has been broken by "
        f"exactly this combination before; ASCII costs nothing here.")


def test_the_firewall_rule_name_is_ascii():
    """The rule name is written into the Windows firewall and read back by the
    verification step. A dash that is not a hyphen has already cost a night."""
    m = re.search(r"\$NOME_REGRA\s*=\s*'([^']*)'", source())
    assert m, "cannot find the firewall rule name"
    name = m.group(1)
    assert all(ord(c) < 128 for c in name), (
        f"the firewall rule name {name!r} is not ASCII")


# -- The claim itself, measured ----------------------------------------------

WINDOWS = sys.platform == "win32"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(not (WINDOWS and PWSH),
                    reason="needs Windows and PowerShell to measure the claim")
def test_the_script_computes_the_profile_windows_reports():
    """Lift the real detection block out of the real file, run it, compare.

    This is the only check here that can fail for a reason the text does not
    show, which is why it exists: the text-level version of this file passed
    while the script still answered `Private` on a `Public` network.
    """
    src = source()
    start = src.find("$perfis = @()")
    end = src.find("}) -join ','")
    assert start >= 0 and end > start, (
        "cannot locate the detection block in the rescue script — this test "
        "would be measuring nothing")
    block = src[start:end + len("}) -join ','")]

    probe = block + (
        "\n"
        "$doScript = $perfilRegra\n"
        "$doWindows = @(Get-NetConnectionProfile |\n"
        "    Where-Object { \"$($_.IPv4Connectivity)\" -ne 'Disconnected' } |\n"
        "    ForEach-Object { \"$($_.NetworkCategory)\" } |\n"
        "    Sort-Object -Unique) -join ','\n"
        "Write-Output ('script=' + $doScript)\n"
        "Write-Output ('windows=' + $doWindows)\n"
    )
    r = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True, text=True, timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert r.returncode == 0, (
        f"the detection block does not run:\n{r.stdout}\n{r.stderr}")

    out = dict(
        line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    script_says = out.get("script", "").strip()
    windows_says = out.get("windows", "").strip()

    if not windows_says:
        pytest.skip("this machine reports no connected network profile")

    assert script_says == windows_says, (
        f"the script would create the rule for {script_says!r} while Windows "
        f"reports the machine is on {windows_says!r}. The rule would not apply "
        f"to the network in use, and the script would still say it worked.")


# -- The one entry point has to be the one the script names -------------------

def test_the_script_and_the_package_agree_on_the_entry_point():
    """The first line of the test tells him to open a shortcut BY NAME.

    Tomorrow starts with one instruction: open `Começar o teste do Wavr`. If the
    file is called something else, or is not in the package at all, the test
    does not begin — there is no second thing to try, and that is deliberate:
    one obvious entry point was the whole design.

    Found the hard way. The shortcut existed in the release-candidate folder and
    not on the Desktop, where the script says it is; copying it there is machine
    state and cannot be tested from here, but the NAMES agreeing can, and a
    rename is the drift that actually happens.
    """
    script = REPO / "_local" / "FIRST_USER_TEST.md"
    if not script.is_file():
        pytest.skip("the first-user script is not in this checkout")

    shortcuts = sorted(RC.glob("*.lnk"))
    assert len(shortcuts) == 1, (
        f"the package should offer exactly one thing to click; it has "
        f"{[p.name for p in shortcuts]}")
    name = shortcuts[0].stem

    text = script.read_text(encoding="utf-8", errors="replace")
    opening = text[:1200]
    assert name in opening, (
        f"the package's entry point is {name!r} and the opening of the script "
        f"does not name it. Tomorrow's first instruction would point at "
        f"something that is not there.")
