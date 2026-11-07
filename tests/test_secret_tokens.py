from pathlib import Path

from jarvis.policy.secrets import SecretStore


def test_runtime_token_files_are_redacted_and_rotation_is_picked_up(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    (tmp_path / "api-token").write_text("api-token-123456", encoding="utf-8")
    assert "api-token-123456" not in store.scrub("x api-token-123456 y")

    (tmp_path / "api-token").write_text("rotated-token-abcdef", encoding="utf-8")
    assert "rotated-token-abcdef" not in store.scrub("rotated-token-abcdef")

    (tmp_path / "node-token").write_text("node-token-abcdef", encoding="utf-8")
    assert "node-token-abcdef" not in store.scrub("node-token-abcdef")

    (tmp_path / "anton-token").write_text("anton-token-abcdef", encoding="utf-8")
    assert "anton-token-abcdef" not in store.scrub("anton-token-abcdef")
