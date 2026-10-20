"""Encrypted credential store: secrets in a file, its key in the OS keychain.

``data/vault.json`` holds one entry per credential. The value is encrypted
with AES-256-GCM, and the entry's name is the authenticated data, so a
ciphertext moved onto another name fails to decrypt. The 32-byte key is
created once and kept in the OS keychain (macOS Keychain, Linux Secret
Service, Windows Credential Locker); it is never written to disk. Copying
the vault file to another machine copies nothing readable.

Each entry is also a credential reference: workspace, fingerprint, created,
rotated, expiry and revocation, readable without the key. Revoking an entry
deletes its ciphertext; putting a new value recovers it.

The vault is opt-in and reversible. ``franco secrets migrate`` moves .env
secrets and the Gmail token into it; ``franco secrets unmigrate`` puts them
back. Until a vault exists, Franco reads .env exactly as before.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
KEYCHAIN_SERVICE = "franco"

# Every .env name whose value is a credential. Usernames, hosts and ports
# are settings, not secrets, and stay in .env.
SECRET_ENV_KEYS = (
    "INSTANTLY_API_KEY", "DATAFORSEO_PASSWORD", "LINKEDIN_PASSWORD",
    "HUNTER_API_KEY", "SERPER_API_KEY", "REOON_API_KEY", "ZEROBOUNCE_API_KEY",
    "GMAIL_CLIENT_SECRET", "SMTP_PASSWORD", "IMAP_PASSWORD", "CLOUDFLARE_API_TOKEN",
)
GMAIL_TOKEN = "GMAIL_TOKEN"

logger = logging.getLogger("franco.vault")


class VaultError(RuntimeError):
    """Base class. Messages never contain secret values."""


class VaultLocked(VaultError):
    """The keychain has no key for this vault (or no keychain is available)."""


class CredentialMissing(VaultError):
    pass


class CredentialRevoked(VaultError):
    pass


class CredentialExpired(VaultError):
    pass


class CredentialCorrupt(VaultError):
    """Decryption failed: the file was edited, or the key is not this vault's."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fingerprint(value: str) -> str:
    """Enough to tell two values apart in the UI, not enough to guess one."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:10]


# ── Where the key lives ──


_KEY_CACHE: dict[str, bytes] = {}


class KeychainKey:
    """The vault key, in the OS keychain via ``keyring``. Read once per
    process: macOS may ask the user to allow access, and once is enough."""

    def __init__(self, vault_path: Path):
        digest = hashlib.sha256(str(Path(vault_path).resolve()).encode()).hexdigest()[:16]
        self.account = f"vault-key:{digest}"

    def get(self) -> bytes | None:
        import keyring
        from keyring.errors import KeyringError

        if self.account in _KEY_CACHE:
            return _KEY_CACHE[self.account]
        try:
            stored = keyring.get_password(KEYCHAIN_SERVICE, self.account)
        except KeyringError as e:
            raise VaultLocked(f"the OS keychain is unavailable ({type(e).__name__})") from None
        if not stored:
            return None
        _KEY_CACHE[self.account] = base64.b64decode(stored)
        return _KEY_CACHE[self.account]

    def set(self, key: bytes) -> None:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.set_password(KEYCHAIN_SERVICE, self.account,
                                 base64.b64encode(key).decode())
        except KeyringError as e:
            raise VaultLocked(f"could not store the vault key in the OS keychain "
                              f"({type(e).__name__})") from None
        _KEY_CACHE[self.account] = key

    def delete(self) -> None:
        import keyring
        from keyring.errors import KeyringError, PasswordDeleteError

        _KEY_CACHE.pop(self.account, None)
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, self.account)
        except (PasswordDeleteError, KeyringError):
            pass


class MemoryKey:
    """For tests: a key that lives only in this object."""

    def __init__(self, key: bytes | None = None):
        self.key = key

    def get(self) -> bytes | None:
        return self.key

    def set(self, key: bytes) -> None:
        self.key = key

    def delete(self) -> None:
        self.key = None


# ── The vault ──


class Vault:
    def __init__(self, path: Path, keys=None):
        self.path = Path(path)
        self.keys = keys if keys is not None else KeychainKey(self.path)

    # file I/O

    def exists(self) -> bool:
        return self.path.is_file()

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"version": VERSION, "entries": {}}
        data = json.loads(self.path.read_text())
        if data.get("version") != VERSION:
            raise VaultError(f"unsupported vault version {data.get('version')!r}")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".vault-")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)          # atomic: never a half-written vault
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # key

    @staticmethod
    def key_id(key: bytes) -> str:
        return hashlib.sha256(b"franco-vault-key:" + key).hexdigest()[:16]

    @property
    def _backup(self) -> Path:
        return self.path.with_suffix(".json.bak")

    def _key(self, create: bool = False) -> bytes:
        key = self.keys.get()
        if key is not None:
            self._recover_rotation(key)
        if key is None and create:
            if any(e.get("ct") for e in self._read()["entries"].values()):
                # Creating a fresh key now would orphan every stored value.
                raise VaultLocked("the vault holds secrets but its key is missing "
                                  "from the OS keychain")
            key = os.urandom(32)
            self.keys.set(key)
        if key is None:
            raise VaultLocked("no vault key in the OS keychain")
        return key

    def _recover_rotation(self, key: bytes) -> None:
        """A key rotation that crashed after rewriting the file but before
        the keychain took the new key leaves a backup made under the key the
        keychain still holds. Restore it."""
        if not (self.path.is_file() and self._backup.is_file()):
            return
        current = self._read().get("key_id")
        if current and current != self.key_id(key):
            backup = json.loads(self._backup.read_text())
            if backup.get("key_id") == self.key_id(key):
                os.replace(self._backup, self.path)
                logger.warning("Vault: restored the backup from an interrupted key rotation.")

    @staticmethod
    def _aad(name: str) -> bytes:
        return f"franco-vault:v{VERSION}:{name}".encode()

    def _encrypt(self, key: bytes, name: str, value: str) -> tuple[str, str]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, value.encode(), self._aad(name))
        return base64.b64encode(nonce).decode(), base64.b64encode(ct).decode()

    def _decrypt(self, key: bytes, name: str, entry: dict) -> str:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            return AESGCM(key).decrypt(base64.b64decode(entry["nonce"]),
                                       base64.b64decode(entry["ct"]),
                                       self._aad(name)).decode()
        except (InvalidTag, KeyError, ValueError):
            raise CredentialCorrupt(f"{name} could not be decrypted") from None

    # operations

    def put(self, name: str, value: str, expires_at: str | None = None,
            workspace_id: str = "local") -> dict:
        """Store or rotate a credential. Returns its reference (no value)."""
        if not value:
            raise VaultError(f"refusing to store an empty value for {name}")
        key = self._key(create=True)
        data = self._read()
        old = data["entries"].get(name, {})
        nonce, ct = self._encrypt(key, name, value)
        now = _now()
        data["key_id"] = self.key_id(key)
        data["entries"][name] = {
            "workspace_id": workspace_id, "nonce": nonce, "ct": ct,
            "fingerprint": fingerprint(value), "status": "active",
            "created_at": old.get("created_at", now),
            "rotated_at": now if old else None,
            "revoked_at": None, "expires_at": expires_at,
        }
        self._write(data)
        return self.ref(name)

    def get(self, name: str, now: datetime | None = None) -> str:
        entry = self._read()["entries"].get(name)
        if entry is None:
            raise CredentialMissing(f"{name} is not stored")
        if entry.get("status") == "revoked":
            raise CredentialRevoked(f"{name} was revoked on {entry.get('revoked_at')}")
        expires = entry.get("expires_at")
        if expires:
            when = datetime.fromisoformat(expires)
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            if when <= (now or datetime.now(timezone.utc)):
                raise CredentialExpired(f"{name} expired at {expires}")
        key = self._key()            # may restore an interrupted rotation…
        entry = self._read()["entries"][name]       # …so read the entry after it
        return self._decrypt(key, name, entry)

    def revoke(self, name: str) -> dict:
        """Destroy the stored value; keep the reference as revoked."""
        data = self._read()
        entry = data["entries"].get(name)
        if entry is None:
            raise CredentialMissing(f"{name} is not stored")
        entry.pop("nonce", None)
        entry.pop("ct", None)
        entry.update(status="revoked", revoked_at=_now())
        self._write(data)
        return self.ref(name)

    def delete(self, name: str) -> None:
        data = self._read()
        if data["entries"].pop(name, None) is not None:
            self._write(data)

    def ref(self, name: str) -> dict:
        entry = self._read()["entries"][name]
        return {"name": name, **{k: v for k, v in entry.items() if k not in ("nonce", "ct")}}

    def refs(self) -> list[dict]:
        return [self.ref(n) for n in sorted(self._read()["entries"])]

    def rotate_key(self) -> int:
        """Re-encrypt every active value under a new key. Returns the count."""
        old = self._key()
        data = self._read()
        plain = {n: self._decrypt(old, n, e) for n, e in data["entries"].items() if e.get("ct")}
        new = os.urandom(32)
        for name, value in plain.items():
            data["entries"][name]["nonce"], data["entries"][name]["ct"] = \
                self._encrypt(new, name, value)
        data["key_id"] = self.key_id(new)
        # Backup (old key) → new file → new key → drop backup. A crash at any
        # point leaves either a consistent pair or a backup _key() restores.
        if self.path.is_file():
            self._backup.write_bytes(self.path.read_bytes())
            self._backup.chmod(0o600)
        self._write(data)
        self.keys.set(new)
        self._backup.unlink(missing_ok=True)
        return len(plain)

    def values(self) -> list[str]:
        """Every readable secret, for redaction. Never raises."""
        out = []
        try:
            data = self._read()
            key = self.keys.get()
        except Exception:
            return out
        if key is None:
            return out
        for name, entry in data["entries"].items():
            if entry.get("ct"):
                try:
                    out.append(self._decrypt(key, name, entry))
                except VaultError:
                    pass
        return out


def default_vault() -> Vault:
    from franco.paths import PROJECT_ROOT

    return Vault(PROJECT_ROOT / "data" / "vault.json")


# ── Redaction ──

_REDACT: set[str] = set()
_installed = False
MIN_REDACT_LEN = 6      # shorter values would scrub ordinary words


def register_secret(value: str | None) -> None:
    if value and len(value) >= MIN_REDACT_LEN:
        _REDACT.add(value)


def redact(text: str) -> str:
    for value in sorted(_REDACT, key=len, reverse=True):
        if value in text:
            text = text.replace(value, "***REDACTED***")
    return text


def install_redaction() -> None:
    """Scrub registered secrets from every log record, whatever its handler.

    Done in the record factory so it applies before any handler, filter or
    formatter sees the message, including ones added later (tests, uvicorn).
    """
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if _REDACT:
            try:
                message = record.getMessage()
            except Exception:
                return record
            clean = redact(message)
            if clean != message:
                record.msg, record.args = clean, ()
            if record.exc_info and record.exc_info[1] is not None:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def register_env_secrets(env: dict) -> None:
    """Register every secret-bearing value in a settings mapping."""
    for name in SECRET_ENV_KEYS:
        register_secret(str(env.get(name) or env.get(name.lower()) or ""))


# ── Migration (and its reversal) ──


def migrate_env(vault: Vault, env_file: Path, gmail_token_file: Path) -> list[str]:
    """Move .env secrets and the Gmail token into the vault.

    Each value is written to the vault and read back before it is removed
    from its plaintext home. Returns the names moved.
    """
    lines = env_file.read_text().splitlines() if env_file.is_file() else []
    moved, kept = [], []
    for line in lines:
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if sep and name in SECRET_ENV_KEYS and value:
            vault.put(name, value)
            if vault.get(name) != value:
                raise VaultError(f"read-back of {name} did not match; .env left as is")
            moved.append(name)
            kept.append(f"{name}=")          # blank: the vault holds it now
        else:
            kept.append(line)
    if gmail_token_file.is_file():
        token = gmail_token_file.read_text()
        vault.put(GMAIL_TOKEN, token)
        if vault.get(GMAIL_TOKEN) != token:
            raise VaultError("read-back of the Gmail token did not match")
        moved.append(GMAIL_TOKEN)
    if moved:
        if lines:
            env_file.write_text("\n".join(kept) + "\n")
            env_file.chmod(0o600)
        gmail_token_file.unlink(missing_ok=True)
    return moved


def unmigrate_env(vault: Vault, env_file: Path, gmail_token_file: Path) -> list[str]:
    """Put every readable vault secret back in .env / the token file."""
    current = env_file.read_text().splitlines() if env_file.is_file() else []
    restored, out, seen = [], [], set()
    for line in current:
        name = line.partition("=")[0].strip()
        if name in SECRET_ENV_KEYS:
            try:
                out.append(f"{name}={vault.get(name)}")
                restored.append(name)
                seen.add(name)
                continue
            except CredentialMissing:
                pass
        out.append(line)
    for ref in vault.refs():
        name = ref["name"]
        if name in SECRET_ENV_KEYS and name not in seen and ref["status"] == "active":
            out.append(f"{name}={vault.get(name)}")
            restored.append(name)
    if any(r["name"] == GMAIL_TOKEN and r["status"] == "active" for r in vault.refs()):
        gmail_token_file.parent.mkdir(parents=True, exist_ok=True)
        gmail_token_file.write_text(vault.get(GMAIL_TOKEN))
        gmail_token_file.chmod(0o600)
        restored.append(GMAIL_TOKEN)
    env_file.write_text("\n".join(out) + "\n")
    env_file.chmod(0o600)
    vault.path.unlink(missing_ok=True)
    vault.keys.delete()
    return restored
