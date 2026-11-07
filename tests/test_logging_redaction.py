"""Regression tests for the unregistered-secret logging boundary.

The point is not to detect every imaginable token format (impossible), but to
ensure a pasted credential under an innocuous key such as ``context`` or
``note`` does not land in the JSONL audit trail verbatim.
"""

from __future__ import annotations

from jarvis.logging_setup import redact


def test_unregistered_credential_shapes_are_redacted_in_free_text() -> None:
    openai = "sk-" + "a" * 24
    github = "ghp_" + "b" * 30
    aws = "AKIA" + "C" * 16
    jwt = "eyJhbGciOiJIUzI1NiJ9." + "d" * 20 + "." + "e" * 20
    bearer = "Bearer " + "f" * 32
    payload = {
        "context": f"pasted: {openai} {github} {aws} {jwt}",
        "note": f"Authorization: {bearer}",
        "password": "hunter2hunter2",
        "safe": "ordinary log text",
    }

    out = redact(payload)
    rendered = str(out)
    for secret in (openai, github, aws, jwt, "f" * 32, "hunter2hunter2"):
        assert secret not in rendered
    assert out["safe"] == "ordinary log text"
    assert "***REDACTED***" in rendered


def test_private_key_block_is_redacted() -> None:
    block = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
        "-----END PRIVATE KEY-----"
    )
    out = redact({"context": block})
    assert "BEGIN PRIVATE KEY" not in str(out)
    assert "***REDACTED***" in str(out)
