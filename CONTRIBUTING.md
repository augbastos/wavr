# Contributing

Issues and pull requests are welcome. This is a small project with one
maintainer, so the fastest path is a focused change with a test.

## Getting set up

```bash
cd backend && pip install -e ".[dev]" && cd ..
python -m wavr.serve                      # loopback-only, http://127.0.0.1:8000
```

```bash
cd backend && pytest -q                   # the whole suite, no hardware needed
node mobile/scripts/sync-frontend.mjs     # packages frontend/ into mobile/www/
node --test mobile/test/*.test.js         # companion unit tests
```

Every hardware path is mock-tested. If a change needs a camera, a radar module
or a phone to be testable, that is a design problem with the change, not a
reason to skip the test.

## The invariants

These are not preferences. A pull request that breaks one will be declined
however good the rest of it is:

- **Nothing leaves the machine** except through a path the operator switched on.
  No analytics, no telemetry, no phone-home, no CDN, no external font.
- **Raw sensing stays raw-sensing-shaped.** Frames live in RAM. Raw positions
  are live-only. What crosses a boundary is the derived observation.
- **Credentials go only where the protocol needs them** — never a log, never a
  response body, never the screen.
- **A new source must be mock-testable** without the hardware it represents.

## Two house rules

**A guarantee needs a producer.** The most expensive defects in this repository
have all had the same shape: a check that worked perfectly with nothing on the
other side producing what it checked. A `heartbeat()` with no caller. A QR
address computed and never drawn. A phone advertising a port nothing listened
on. When you add a guarantee, follow it to the thing that produces it and make
sure something calls it.

**Measure, don't guess.** "It feels slow", "it doesn't find it", "the layout is
ugly" — each has a specific cause. A wrong hypothesis costs more than the
measurement would have, and this project has the scar tissue to prove it.

## Tests

Test files are named for what they guarantee, not for what they touch —
`test_the_screen_never_invents_a_house.py`, not `test_housemap.py` — and the
docstring says which failure it exists to prevent, in words a person could have
used to report it.

A test that skips is not a test that passed. If yours can only run in some
environments, make the skip loud and specific about what is missing.

## Pull requests

- Keep it to one thing. A rename plus a fix is two reviews in one diff.
- Green suite. `pytest -q` from `backend/`.
- Explain the failure, not just the change. The commit message should let
  somebody understand what went wrong without reading the diff.
- No new mandatory dependency without a reason the lazy-extras pattern cannot
  cover.

### What enforces this, and what does not

Nothing on the server side stops a red change reaching `master`. Branch
protection and rulesets both answer `403 — Upgrade to GitHub Pro or make this
repository public` on this account, so the branch cannot be protected while the
repository is private. That is stated here rather than papered over: assume
`master` is writable, and treat the checks as something you read rather than
something that stops you.

What the repository does enforce:

- `tests` and `scpe` run on every pull request, with no path filter, so the six
  test jobs and the disclosure check always have a verdict on the head commit.
  `docker` and `install-matrix` are path-filtered and legitimately produce no
  check run for changes outside their paths.
- `tests` also runs on every push to `master`, so a red `master` is visible
  within minutes even though nothing prevented it.
- The maintenance auto-merge workflow refuses to merge while ANY check on that
  exact head SHA is unfinished or not green — it excludes only itself, and a
  path-filtered workflow that produced no check run counts as inapplicable
  rather than as a failure.

## Good first contributions

A new `SensorSource` — a Zigbee occupancy sensor, another BLE beacon type, a
presence signal from something already on your LAN. The interface is small, the
mock pattern is established, and every one of them makes the fusion better at
the thing it exists to do.

## Security

Do not open a public issue for anything exploitable. See [SECURITY.md](SECURITY.md).
