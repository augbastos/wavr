"""The native client command contract: how a native app asks the Core to act.

The client snapshot (`client_view.py`) is the read side. This is the write side:
pairing, approvals, privacy switches, sources, settings. A native client must
not hand-craft HTTP for these -- every platform would re-derive the paths, the
body shapes, the auth headers and the meaning of each error, and drift.

So each command is one row of DATA in `COMMANDS`: a name, the existing Core
route it maps to, and the arguments it takes. Nothing here is a second API: every
row is a route the dashboard already uses, and the Core stays the only place
that decides who may do what (roles, scopes, CSRF) and whether a value is
acceptable. This module checks only the SHAPE of the arguments -- a missing
field, a string where a bool belongs -- so a malformed call fails before it
leaves the device, and never checks a business rule the Core owns.

`native/` interprets the same table (generated into
`native/src/client_commands_table.inc` by `scripts/gen_conformance.py`) and is
held to this module by `conformance/client_commands.json`:

  request_for(name, args)  -> {method, path, body, auth} | raises CommandError
  result_for(status, body) -> CommandResult {ok, status, error, detail, data}
  transport_failure(msg)   -> CommandResult for "the Core did not answer"

Error kinds are stable strings a UI can switch on; `detail` is the Core's own
sentence when it gave one (FastAPI's `detail`), shown as-is, never invented.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

SCHEMA = 1

# Argument types. "any" is a JSON value passed through untouched (a setting's
# value is typed by the Core, not here).
_TYPES = ("string", "bool", "int", "strings", "any")


def _arg(type_, *, required=True, where="body"):
    assert type_ in _TYPES and where in ("path", "body")
    return {"type": type_, "required": required, "in": where}


def _cmd(method, path, auth="token", **args):
    return {"method": method, "path": path, "auth": auth, "args": args}


# auth "none": an onboarding route a device reaches BEFORE it holds a token. The
# token is never sent on these, even when the caller has one.
COMMANDS: dict[str, dict] = {
    # Joining a Space from a new device: approve-on-the-Core (no code to type)...
    "pair.request": _cmd("POST", "/api/pair-request", auth="none",
                         requester_name=_arg("string"),
                         platform=_arg("string", required=False),
                         reported_fp=_arg("string", required=False),
                         device_key=_arg("string", required=False)),
    "pair.status": _cmd("POST", "/api/pair-request/status", auth="none",
                        request_id=_arg("string")),
    # ...or a one-time code shown on the Core.
    "pair.redeem": _cmd("POST", "/api/pair", auth="none",
                        code=_arg("string"), device_name=_arg("string"),
                        device_key=_arg("string", required=False)),
    # On the Core: let a device in, or mint a code for one.
    "pairings.list": _cmd("GET", "/api/pending-pairings"),
    "pairings.approve": _cmd("POST", "/api/pending-pairings/{id}/approve",
                             id=_arg("string", where="path"),
                             role=_arg("string", required=False),
                             confirm_code=_arg("string")),
    "pairings.deny": _cmd("POST", "/api/pending-pairings/{id}/deny",
                          id=_arg("string", where="path")),
    "pair.code": _cmd("POST", "/api/pair-code",
                      role=_arg("string", required=False),
                      person_id=_arg("string", required=False)),
    "devices.list": _cmd("GET", "/api/devices"),
    "device.revoke": _cmd("DELETE", "/api/devices/{id}", id=_arg("string", where="path")),
    "device.role": _cmd("POST", "/api/devices/{id}/role",
                        id=_arg("string", where="path"), role=_arg("string")),
    # Privacy and sensing.
    "watch.get": _cmd("GET", "/api/watch"),
    "watch.set": _cmd("POST", "/api/watch", on=_arg("bool")),
    "sensing.set": _cmd("POST", "/api/system/toggle", on=_arg("bool")),
    "sources.list": _cmd("GET", "/api/system"),
    "source.set": _cmd("POST", "/api/sources/{name}/toggle",
                       name=_arg("string", where="path"), enabled=_arg("bool")),
    "source.restart": _cmd("POST", "/api/sources/{name}/restart",
                           name=_arg("string", where="path")),
    # Sensor Nodes (loopback-root on the Core).
    "nodes.list": _cmd("GET", "/api/nodes"),
    "nodes.pending": _cmd("GET", "/api/nodes/pending"),
    "node.approve": _cmd("POST", "/api/nodes/{id}/approve",
                         id=_arg("string", where="path"), name=_arg("string"),
                         sensor_type=_arg("string"), room=_arg("string"),
                         transport=_arg("string", required=False)),
    "node.deny": _cmd("POST", "/api/nodes/{id}/deny", id=_arg("string", where="path")),
    "node.disable": _cmd("POST", "/api/nodes/{id}/disable", id=_arg("string", where="path")),
    "node.revoke": _cmd("DELETE", "/api/nodes/{id}", id=_arg("string", where="path")),
    "node.enroll_code": _cmd("POST", "/api/nodes/enroll-code",
                             name=_arg("string"), sensor_type=_arg("string"),
                             room=_arg("string"), transport=_arg("string", required=False)),
    # The Space, its settings, first run.
    "space.get": _cmd("GET", "/api/space"),
    "space.rename": _cmd("PUT", "/api/space", name=_arg("string"),
                         kind=_arg("string", required=False)),
    "settings.list": _cmd("GET", "/api/settings"),
    "setting.set": _cmd("PUT", "/api/settings/{key}", key=_arg("string", where="path"),
                        value=_arg("any"), consent=_arg("string", required=False)),
    "setting.reset": _cmd("DELETE", "/api/settings/{key}", key=_arg("string", where="path")),
    "setup.status": _cmd("GET", "/api/setup/status"),
    "setup.create_space": _cmd("POST", "/api/setup/create-space", name=_arg("string"),
                               kind=_arg("string", required=False),
                               owner_name=_arg("string", required=False),
                               functions=_arg("strings", required=False),
                               room=_arg("string", required=False)),
    # Things the Core found and is asking about.
    "discoveries.list": _cmd("GET", "/api/discoveries"),
    "discovery.accept": _cmd("POST", "/api/discoveries/{id}/accept",
                             id=_arg("string", where="path")),
    "discovery.dismiss": _cmd("POST", "/api/discoveries/{id}/dismiss",
                              id=_arg("string", where="path")),
    # The Space drawn: geometry and verdicts for a renderer (space_scene.py).
    "scene.get": _cmd("GET", "/api/scene"),
    # Diagnosis. Read-only: auto_fix is deliberately not exposed (it is a GET that
    # changes state, and belongs on the Core's own screen).
    "doctor.run": _cmd("GET", "/api/health/doctor"),
}

# Status -> the one word a UI switches on. Anything else in 4xx is "refused",
# anything in 5xx is "server".
_ERRORS = {400: "invalid", 401: "unauthorized", 403: "forbidden", 404: "not_found",
           409: "conflict", 422: "invalid", 423: "locked", 429: "throttled"}

_PATH_ARG = re.compile(r"\{([a-z_]+)\}")


class CommandError(ValueError):
    """The call is malformed; it never left the device. `code` is stable:
    unknown_command, not_object, unknown_argument, missing_argument,
    wrong_type, bad_path_segment."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _type_ok(type_: str, v) -> bool:
    if type_ == "string":
        return isinstance(v, str)
    if type_ == "bool":
        return isinstance(v, bool)
    if type_ == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if type_ == "strings":
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    return True   # "any"


def segment(v: str) -> str:
    """One path segment, percent-encoded so an argument can never add a segment
    ('../'), a query ('?') or a fragment: everything but RFC 3986 unreserved
    characters is escaped, byte by byte, from UTF-8."""
    return quote(v, safe="")


def request_for(name, args) -> dict:
    """The HTTP request a command is, or CommandError. `args` is a dict."""
    spec = COMMANDS.get(name) if isinstance(name, str) else None
    if spec is None:
        raise CommandError("unknown_command", f"unknown command: {name!r}")
    if not isinstance(args, dict):
        raise CommandError("not_object", "arguments must be a JSON object")
    unknown = sorted(k for k in args if k not in spec["args"])
    if unknown:
        raise CommandError("unknown_argument", f"{name}: unknown argument {unknown[0]!r}")
    body = {}
    path_values = {}
    # Sorted, so the first fault found is the same in every implementation
    # (native/ reads the table into an ordered map).
    for key in sorted(spec["args"]):
        a = spec["args"][key]
        if key not in args or args[key] is None:
            if a["required"]:
                raise CommandError("missing_argument", f"{name}: missing argument {key!r}")
            continue
        v = args[key]
        if not _type_ok(a["type"], v):
            raise CommandError("wrong_type", f"{name}: argument {key!r} must be {a['type']}")
        if a["in"] == "path":
            # "." and ".." survive percent-encoding and are dot-segments to
            # anything that normalises a path on the way.
            if v in ("", ".", ".."):
                raise CommandError("bad_path_segment", f"{name}: argument {key!r} is not a valid path segment")
            path_values[key] = segment(v)
        else:
            body[key] = v
    path = _PATH_ARG.sub(lambda m: path_values[m.group(1)], spec["path"])
    return {"method": spec["method"], "path": path,
            "body": body if spec["method"] != "GET" else None, "auth": spec["auth"]}


def result_for(status, body_text) -> dict:
    """What the Core's answer means, for a command whose request was sent."""
    try:
        data = json.loads(body_text) if isinstance(body_text, str) and body_text else None
    except ValueError:
        data = None
    if isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300:
        return {"ok": True, "status": status, "error": None, "detail": None, "data": data}
    if not isinstance(status, int) or isinstance(status, bool):
        return transport_failure("no HTTP status")
    detail = data.get("detail") if isinstance(data, dict) else None
    kind = _ERRORS.get(status) or ("server" if status >= 500 else "refused")
    return {"ok": False, "status": status, "error": kind,
            "detail": detail if isinstance(detail, str) else None, "data": data}


def transport_failure(message: str) -> dict:
    return {"ok": False, "status": None, "error": "unreachable",
            "detail": message if isinstance(message, str) else None, "data": None}


def table() -> dict:
    """The command table as the native runtime embeds it."""
    return {"schema": SCHEMA, "commands": COMMANDS}
