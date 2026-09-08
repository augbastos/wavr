"""The shell may not hand out a Core that has never executed an instruction.

## What happened

On Windows the sidecar is spawned `CREATE_SUSPENDED`. That is deliberate and
load-bearing: the child's single thread exists but has not run, so the shell can
assign it to a Job Object before it can spawn children of its own and escape.
`confine_backend_to_job_object()` does the assigning and then always resumes it.

Calling that function was the CALLER's job, and there were three callers: first
launch, the crash watchdog, and the tray's "Restart Core". Two remembered.

The third spawned the child, stored the handle, wrote
`Wavr: Core restarted from the tray.` to the log, and returned. The process sat
there with one thread in `WaitReason=Suspended`, `TotalProcessorTime` of exactly
zero, no listening socket and not even a PyInstaller extraction directory —
because it had never run far enough to make one. The first user pressed Restart
Core at step E2 and watched the dashboard say "reconnecting..." at a Core that
had never started, while the same screen's Space Status card said "Everything
looks normal."

## What is pinned here, and why it is not "the call is present"

Asserting that `restart_backend` contains the call would pin the fix and not the
rule; the fourth caller would be free to forget again. What is pinned is the
shape that makes forgetting impossible: `spawn_backend()` confines and resumes
before it returns, and nobody else calls the confine function at all.

Read from the Rust source because there is no way to run it from here. Comments
are stripped first — this file's own explanation names every symbol it checks
for, and a check that reads prose finds the defect inside the warning against
it. That has happened twice in this repository already.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

MAIN = (Path(__file__).resolve().parents[2] / "desktop" / "src-tauri"
        / "src" / "main.rs")


@pytest.fixture(scope="module")
def codigo() -> str:
    if not MAIN.is_file():
        pytest.skip(f"{MAIN} is not in this checkout")
    fonte = io.open(MAIN, encoding="utf-8", newline="").read()
    fonte = re.sub(r"/\*.*?\*/", " ", fonte, flags=re.S)
    return re.sub(r"^\s*//.*$", "", fonte, flags=re.M)


def _corpo(codigo: str, assinatura: str) -> str:
    """The body of one function, by brace balance."""
    inicio = codigo.index(assinatura)
    abre = codigo.index("{", inicio)
    nivel = 0
    for i in range(abre, len(codigo)):
        if codigo[i] == "{":
            nivel += 1
        elif codigo[i] == "}":
            nivel -= 1
            if nivel == 0:
                return codigo[abre:i + 1]
    raise AssertionError(f"unbalanced braces after {assinatura}")


def test_the_spawn_confines_and_resumes_before_it_returns(codigo):
    corpo = _corpo(codigo, "fn spawn_backend()")
    assert "CREATE_SUSPENDED" in corpo, (
        "the child is no longer started suspended — if that is deliberate the "
        "Job Object race is back, and this whole file should be re-read")
    assert "confine_backend_to_job_object" in corpo, (
        "spawn_backend() hands out a suspended child and leaves confining and "
        "resuming it to whoever remembers")
    # Order matters: spawning and then returning without confining is the bug.
    assert corpo.index("cmd.spawn()") < corpo.index("confine_backend_to_job_object"), (
        "the child is returned before it is confined and resumed")


def test_no_caller_is_trusted_to_confine_the_child(codigo):
    """One call site, inside the spawn. Not two, not three."""
    # `(?<!fn )` because the definition's own signature matches the call shape,
    # and counting it would put the ceiling one too high — which would have let
    # exactly the defect this file is about slip through as "one call site".
    chamadas = re.findall(r"(?<!fn )\bconfine_backend_to_job_object\s*\(", codigo)
    definicao = re.findall(r"fn confine_backend_to_job_object", codigo)
    assert len(definicao) == 1, "the confine function was renamed or duplicated"
    assert len(chamadas) == 1, (
        f"confine_backend_to_job_object is called from {len(chamadas)} places. "
        "It belongs inside spawn_backend() and nowhere else — every extra call "
        "site is another place a future caller can forget, which is exactly how "
        "the tray shipped a Core that never ran.")
    dentro = _corpo(codigo, "fn spawn_backend()")
    assert "confine_backend_to_job_object" in dentro, (
        "the single call site is not inside spawn_backend()")


def test_the_confine_always_resumes_whatever_else_fails(codigo):
    """The suspension is a means; the resume is not conditional on the means
    working. A Job Object step that fails must still leave a running Core."""
    corpo = _corpo(codigo, "fn confine_backend_to_job_object")
    assert "resume_suspended_process" in corpo
    depois_do_ultimo_erro = corpo.rsplit("Err(msg)", 1)[-1]
    assert "resume_suspended_process" in depois_do_ultimo_erro, (
        "the resume no longer runs after the error branch, so a failed Job "
        "Object step leaves the Core suspended forever")
    # Textual order is not enough, and this check exists because a mutation
    # proved it: adding `return;` inside the error branch leaves the resume
    # sitting below it, unreachable, and the order assertion above still
    # passed. The function's own design note states the rule — "a closure (not
    # early returns directly in the function body) so there is exactly ONE call
    # to resume_suspended_process" — so the rule is what gets checked.
    assert not re.search(r"\breturn\b", corpo), (
        "confine_backend_to_job_object has an early return. Every path through "
        "it must reach resume_suspended_process, or a Job Object failure leaves "
        "a Core that never executes an instruction — which is what the tray "
        "shipped.")


def test_the_tray_restart_says_what_happened_not_what_was_attempted(codigo):
    """The log line that was wrong twice in one day.

    It was written the instant the spawn call returned — before the Core had
    started, and in one case before it had run at all. Both times the log
    claimed a restart while the dashboard sat on "reconnecting...".
    """
    corpo = _corpo(codigo, "fn restart_backend")
    assert "wait_healthy" in corpo, (
        "restart_backend does not wait for the Core to answer, so whatever it "
        "logs is a claim about the spawn call and not about the Core")
    sucesso = corpo.index("wait_healthy")
    positivo = re.search(r'log_issue\(\s*"Wavr: Core restarted[^"]*"', corpo)
    assert positivo, "the success line was renamed; check it is still guarded"
    assert positivo.start() > sucesso, (
        "the success line is written before the health check, which is what "
        "made it a lie")
    assert "NOT answering" in corpo or "not answering" in corpo, (
        "there is no failure branch: a restart that does not come back must "
        "say so, in the log and to the person who pressed the button")
