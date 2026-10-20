"""P01-A3: expired credentials, revocation, recovery and secrets redaction.

Every test uses a temporary vault and an in-memory (or fake-keyring) key:
nothing touches the real OS keychain or data/.
"""
import json
import logging
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from franco import capabilities as caps
from franco import vault as V

SECRET = "sk-live-0123456789abcdef"


@pytest.fixture(autouse=True)
def no_real_token_file(tmp_path, monkeypatch):
    """No test in this module may touch the checkout's data/ folder."""
    from franco.integrations import gmail as G

    monkeypatch.setattr(G, "TOKEN_FILE", tmp_path / "real-data" / "gmail_token.json")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    store = V.Vault(tmp_path / "data" / "vault.json", keys=V.MemoryKey())
    monkeypatch.setattr(V, "default_vault", lambda: store)
    return store


@pytest.fixture
def isolated_env(monkeypatch):
    import franco.config as config_module

    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    for name in V.SECRET_ENV_KEYS:
        monkeypatch.setenv(name, "")


# ── Storage ──


def test_values_are_encrypted_at_rest_and_the_file_is_private(vault):
    ref = vault.put("SERPER_API_KEY", SECRET)
    raw = vault.path.read_text()
    assert SECRET not in raw and "nonce" in raw
    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600
    assert vault.get("SERPER_API_KEY") == SECRET
    assert ref["fingerprint"].startswith("sha256:") and SECRET not in json.dumps(ref)
    assert vault.keys.key not in raw.encode()


def test_ciphertext_is_bound_to_its_name_and_tampering_is_detected(vault):
    vault.put("A_KEY", "first-secret")
    vault.put("B_KEY", "second-secret")
    data = json.loads(vault.path.read_text())
    data["entries"]["A_KEY"]["ct"], data["entries"]["B_KEY"]["ct"] = \
        data["entries"]["B_KEY"]["ct"], data["entries"]["A_KEY"]["ct"]
    data["entries"]["A_KEY"]["nonce"], data["entries"]["B_KEY"]["nonce"] = \
        data["entries"]["B_KEY"]["nonce"], data["entries"]["A_KEY"]["nonce"]
    vault.path.write_text(json.dumps(data))
    with pytest.raises(V.CredentialCorrupt):
        vault.get("A_KEY")


def test_missing_key_locks_the_vault_and_never_orphans_secrets(vault):
    vault.put("SERPER_API_KEY", SECRET)
    vault.keys.key = None                               # keychain lost the key
    with pytest.raises(V.VaultLocked):
        vault.get("SERPER_API_KEY")
    with pytest.raises(V.VaultLocked):                  # would orphan the old value
        vault.put("OTHER", "another-secret")


# ── P01-A3: expiry, revocation, recovery ──


def test_expired_credential_is_refused_then_recovered(vault):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    vault.put("SERPER_API_KEY", SECRET, expires_at=past)
    with pytest.raises(V.CredentialExpired):
        vault.get("SERPER_API_KEY")
    vault.put("SERPER_API_KEY", "sk-live-rotated-value")          # recovery
    assert vault.get("SERPER_API_KEY") == "sk-live-rotated-value"
    assert vault.ref("SERPER_API_KEY")["rotated_at"]


def test_revocation_destroys_the_value_and_recovery_restores_it(vault):
    vault.put("SERPER_API_KEY", SECRET)
    ref = vault.revoke("SERPER_API_KEY")
    assert ref["status"] == "revoked" and ref["revoked_at"]
    entry = json.loads(vault.path.read_text())["entries"]["SERPER_API_KEY"]
    assert "ct" not in entry and "nonce" not in entry       # gone, not hidden
    with pytest.raises(V.CredentialRevoked):
        vault.get("SERPER_API_KEY")
    vault.put("SERPER_API_KEY", "sk-live-new-value")
    assert vault.ref("SERPER_API_KEY")["status"] == "active"
    assert vault.get("SERPER_API_KEY") == "sk-live-new-value"


def test_key_rotation_reencrypts_and_survives_a_crash_midway(vault):
    vault.put("SERPER_API_KEY", SECRET)
    vault.put("SMTP_PASSWORD", "smtp-password-1")
    old_key = vault.keys.key
    assert vault.rotate_key() == 2
    assert vault.keys.key != old_key
    assert vault.get("SERPER_API_KEY") == SECRET

    # Crash after the file is rewritten but before the keychain takes the key.
    before = vault.keys.key
    real_set = vault.keys.set

    def crash(_key):
        raise KeyboardInterrupt("power cut")

    vault.keys.set = crash
    with pytest.raises(KeyboardInterrupt):
        vault.rotate_key()
    vault.keys.set = real_set
    assert vault.keys.key == before                     # keychain kept the old key
    assert vault.get("SMTP_PASSWORD") == "smtp-password-1"   # backup restored
    assert not vault._backup.exists()


def test_revoked_or_expired_credentials_make_capabilities_unavailable(vault, isolated_env):
    from franco.config import load_env

    vault.put("SERPER_API_KEY", SECRET)
    env = load_env()
    assert env.serper_api_key == SECRET and env.credential_problems == {}
    assert caps.evaluate("discovery.serper", env.model_dump()).executable

    vault.revoke("SERPER_API_KEY")
    import os
    os.environ["SERPER_API_KEY"] = ""
    env = load_env()
    assert env.serper_api_key == "" and env.credential_problems == {"SERPER_API_KEY": "revoked"}
    status = caps.evaluate("discovery.serper", env.model_dump())
    assert not status.executable and "revoked in the vault" in status.reason


# ── Redaction ──


def test_secrets_are_redacted_from_every_log_record(vault, isolated_env, caplog):
    from franco.config import load_env

    vault.put("SMTP_PASSWORD", "hunter2-smtp-pass")
    load_env()
    log = logging.getLogger("franco.test")
    with caplog.at_level(logging.INFO):
        log.error("login failed for user with password %s", "hunter2-smtp-pass")
        log.warning(f"url https://api.x/?key=hunter2-smtp-pass&q=1")
        try:
            raise RuntimeError("auth header Bearer hunter2-smtp-pass rejected")
        except RuntimeError:
            log.exception("provider call failed")
    text = caplog.text
    assert "hunter2-smtp-pass" not in text
    assert text.count("***REDACTED***") >= 3


def test_gmail_token_lives_in_the_vault_and_is_redacted(vault, isolated_env, caplog):
    from franco.integrations import gmail as G

    token = {"refresh_token": "1//refresh-token-xyz", "access_token": "ya29.access-abc",
             "expires_at": 0}
    vault.put("HUNTER_API_KEY", "hunter-key-000000")       # a vault is in use
    G._save_token(token)
    assert not G.TOKEN_FILE.exists()                      # never written in plaintext
    assert "1//refresh-token-xyz" not in vault.path.read_text()
    assert G._load_token()["refresh_token"] == "1//refresh-token-xyz"
    with caplog.at_level(logging.INFO):
        logging.getLogger("franco.gmail").info("refreshing with 1//refresh-token-xyz")
    assert "1//refresh-token-xyz" not in caplog.text


# ── Migration and its reversal ──


def test_migrate_and_unmigrate_round_trip(vault, tmp_path):
    env_file = tmp_path / ".env"
    token_file = tmp_path / "data" / "gmail_token.json"
    env_file.write_text("SMTP_HOST=smtp.x.co\nSMTP_PASSWORD=pw-123456\n"
                        "SERPER_API_KEY=serper-abcdef\n# comment\n")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text('{"refresh_token": "1//r"}')

    moved = V.migrate_env(vault, env_file, token_file)
    assert set(moved) == {"SMTP_PASSWORD", "SERPER_API_KEY", V.GMAIL_TOKEN}
    assert env_file.read_text() == "SMTP_HOST=smtp.x.co\nSMTP_PASSWORD=\nSERPER_API_KEY=\n# comment\n"
    assert not token_file.exists()
    assert vault.get("SMTP_PASSWORD") == "pw-123456"

    restored = V.unmigrate_env(vault, env_file, token_file)
    assert set(restored) == set(moved)
    assert env_file.read_text() == ("SMTP_HOST=smtp.x.co\nSMTP_PASSWORD=pw-123456\n"
                                    "SERPER_API_KEY=serper-abcdef\n# comment\n")
    assert json.loads(token_file.read_text())["refresh_token"] == "1//r"
    assert not vault.exists() and vault.keys.key is None


# ── The keychain adapter ──


def test_keychain_adapter_uses_keyring_and_locks_on_backend_errors(tmp_path, monkeypatch):
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import KeyringError

    class FakeBackend(KeyringBackend):
        priority = 1
        broken = False

        def __init__(self):
            super().__init__()
            self.store = {}

        def get_password(self, service, user):
            if self.broken:
                raise KeyringError("locked")
            return self.store.get((service, user))

        def set_password(self, service, user, password):
            self.store[(service, user)] = password

        def delete_password(self, service, user):
            self.store.pop((service, user), None)

    backend = FakeBackend()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(V, "_KEY_CACHE", {})
    try:
        store = V.Vault(tmp_path / "vault.json")
        store.put("SERPER_API_KEY", SECRET)
        (service, account), = backend.store
        assert service == "franco" and account.startswith("vault-key:")
        assert SECRET not in json.dumps(list(backend.store.values()))
        V._KEY_CACHE.clear()
        backend.broken = True
        with pytest.raises(V.VaultLocked):
            store.get("SERPER_API_KEY")
    finally:
        keyring.set_keyring(previous)


# ── Dashboard ──


def test_dashboard_stores_secrets_in_the_vault_and_never_returns_them(
        vault, isolated_env, tmp_path, monkeypatch):
    from franco import dashboard

    monkeypatch.setattr(dashboard, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "f.db")
    vault.put("HUNTER_API_KEY", "hunter-key-000000")      # vault in use
    client = TestClient(dashboard.app, base_url="http://localhost")

    assert client.post("/api/settings/env", json={"SMTP_HOST": "smtp.x.co",
                       "SMTP_PASSWORD": "smtp-secret-999"}).json()["success"]
    assert "smtp-secret-999" not in (tmp_path / ".env").read_text()
    assert vault.get("SMTP_PASSWORD") == "smtp-secret-999"

    for path in ("/api/secrets", "/api/settings", "/api/setup-status", "/api/capabilities"):
        body = client.get(path).text
        assert "smtp-secret-999" not in body and "hunter-key-000000" not in body, path
    assert client.get("/api/settings").json()["smtp_password_set"] is True

    assert client.post("/api/secrets/SMTP_PASSWORD/revoke",
                       headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/secrets/NOPE/revoke").status_code == 404
    assert client.post("/api/secrets/SMTP_PASSWORD/revoke").json()["ref"]["status"] == "revoked"
    refs = {r["name"]: r for r in client.get("/api/secrets").json()["refs"]}
    assert refs["SMTP_PASSWORD"]["status"] == "revoked"
