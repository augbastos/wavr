"""Test isolation from the operator's local .env.

`wavr.config` calls `load_dotenv()` once at import, which populates os.environ from
the developer's `./.env` (WAVR_* flags, LLM keys). Tests assert against the getenv
DEFAULTS and must not be perturbed by whatever features the operator has enabled on
their own machine. This autouse fixture clears those variables before every test;
a test that needs a specific value still sets it explicitly with
`monkeypatch.setenv`, which runs after this fixture. `load_dotenv` is import-time
only, so cleared values are not re-read during the test.
"""
import os

import pytest

_ISOLATED_KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _isolate_wavr_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("WAVR_") or key in _ISOLATED_KEYS:
            monkeypatch.delenv(key, raising=False)
