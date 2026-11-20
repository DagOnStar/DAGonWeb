"""Run workflows on an HPC cluster as each user's own Unix account.

DAGonWeb never gains privileges on the cluster. A user links a Unix account
by adding a restricted public key to their own ``~/.ssh/authorized_keys``
(only that Unix user can write the file, so a successful SSH login with the
key proves the claim). The key's forced command is ``dagonweb-runner``
(``hpc/dagonweb_runner.py``), so the key can run nothing else, and every
request is one of a fixed set of verbs.

* Key pairs are generated here (Ed25519); the private half is encrypted with
  Fernet using ``HPC_KEY_SECRET`` (environment) or, when unset, a key derived
  from ``SECRET_KEY``. It never reaches the browser or the logs.
* The login node's host key is pinned in the ``hpc_ssh_host_key`` setting;
  unknown host keys are always rejected.
* A run always executes as the Unix account of the user who submits it.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from flask import current_app

from . import settings
from .models import User

UNIX_USERNAME = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
# Never linkable, whatever the uid check on the cluster says.
RESERVED_USERNAMES = frozenset({
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp", "proxy", "www-data", "backup",
    "list", "irc", "gnats", "nobody", "systemd-network", "systemd-resolve", "messagebus", "sshd", "slurm", "munge",
    "admin", "adm", "operator", "postgres", "mysql", "redis", "docker", "dagonweb",
})


class HpcError(RuntimeError):
    """The cluster refused or could not complete a request."""


# ---------------------------------------------------------------- keys


def _fernet():
    from cryptography.fernet import Fernet

    secret = current_app.config.get("HPC_KEY_SECRET") or ""
    if secret:
        try:
            return Fernet(secret.encode() if isinstance(secret, str) else secret)
        except (ValueError, TypeError):
            pass  # not a Fernet key: derive one from the passphrase below
    material = (secret or current_app.config["SECRET_KEY"]).encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dagonweb-hpc-keys\0" + material).digest()))


def encrypt_private_key(pem: str) -> str:
    return _fernet().encrypt(pem.encode("utf-8")).decode("ascii")


def decrypt_private_key(token: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise HpcError("The stored cluster key cannot be decrypted (was HPC_KEY_SECRET or SECRET_KEY changed?). Link the account again.") from exc


def generate_keypair(comment: str) -> tuple[str, str]:
    """Return ``(private OpenSSH PEM, public key line)`` for a new Ed25519 key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode("ascii")
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")
    return private, f"{public} {comment}"


def key_comment(user: User) -> str:
    return f"dagonweb-account-{user.id}"


def authorized_keys_line(user: User) -> str:
    """The exact line the user appends to ``~/.ssh/authorized_keys``."""
    options = ["restrict"]
    cidr = ",".join(item.strip() for item in str(settings.get("hpc_source_cidr") or "").split(",") if item.strip())
    if cidr:
        options.append(f'from="{cidr}"')
    options.append(f'command="{settings.get("hpc_runner_command")}"')
    return f"{','.join(options)} {user.unix_public_key}"


def validate_username(name: str) -> str:
    name = (name or "").strip()
    if not UNIX_USERNAME.match(name):
        raise ValueError("Enter your cluster user name: lower-case letters, digits, '.', '_' or '-', at most 32 characters.")
    if name in RESERVED_USERNAMES:
        raise ValueError("System accounts cannot be linked.")
    return name


def start_link(user: User, name: str) -> None:
    """Claim a Unix user name and issue a fresh key pair (the caller commits).

    A claim is only a claim: several DAGonWeb accounts may claim the same
    name, but only one can prove it, and a proven link wins.
    """
    name = validate_username(name)
    owner = User.query.filter(User.unix_username == name, User.unix_linked_at.isnot(None), User.id != user.id).first()
    if owner is not None:
        raise ValueError("This cluster account is already linked to another DAGonWeb account.")
    private, public = generate_keypair(key_comment(user))
    user.unix_username = name
    user.unix_public_key = public
    user.unix_private_key = encrypt_private_key(private)
    user.unix_linked_at = None


def unlink(user: User) -> None:
    user.unix_username = None
    user.unix_public_key = None
    user.unix_private_key = None
    user.unix_linked_at = None


def verify_link(user: User) -> dict[str, Any]:
    """Prove the claim by signing in as the Unix user; set ``unix_linked_at``."""
    if not (user.unix_username and user.unix_private_key):
        raise HpcError("Choose your cluster user name first.")
    owner = User.query.filter(User.unix_username == user.unix_username, User.unix_linked_at.isnot(None), User.id != user.id).first()
    if owner is not None:
        raise HpcError("This cluster account is already linked to another DAGonWeb account.")
    with client_for(user, require_linked=False) as client:
        info = client.ping()
    user.unix_linked_at = datetime.now(timezone.utc)
    return info


# ---------------------------------------------------------------- transport


class Transport(Protocol):
    def exec(self, command: str, stdin: bytes | None = None, max_output: int = 0) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


def parse_host_key(line: str) -> tuple[str, str]:
    parts = line.strip().split()
    if len(parts) >= 3 and not parts[0].startswith(("ssh-", "ecdsa-")):
        parts = parts[1:]  # known_hosts format: "host keytype base64"
    if len(parts) < 2:
        raise HpcError("The login node host key is not configured (Administration → HPC cluster).")
    return parts[0], parts[1]


class SSHTransport:
    """Paramiko connection to the login node, opened lazily and reused."""

    def __init__(self, host: str, port: int, username: str, private_key: str, host_key: str, timeout: float = 20) -> None:
        self.host, self.port, self.username, self.timeout = host, port, username, timeout
        self.private_key, self.host_key = private_key, host_key
        self.client: Any = None

    def _connect(self) -> Any:
        import paramiko

        if self.client is not None:
            return self.client
        key_type, key_data = parse_host_key(self.host_key)
        try:
            pinned = paramiko.PKey.from_type_string(key_type, base64.b64decode(key_data))
        except Exception as exc:  # noqa: BLE001 - paramiko raises several types
            raise HpcError(f"The configured login node host key is invalid: {exc}") from exc
        client = paramiko.SSHClient()
        for name in {self.host, f"[{self.host}]:{self.port}"}:
            client.get_host_keys().add(name, key_type, pinned)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                self.host, port=self.port, username=self.username,
                pkey=paramiko.Ed25519Key.from_private_key(io.StringIO(self.private_key)),
                allow_agent=False, look_for_keys=False, timeout=self.timeout, banner_timeout=self.timeout, auth_timeout=self.timeout,
            )
        except paramiko.BadHostKeyException as exc:
            raise HpcError("The login node presented a different host key than the configured one. Connection refused.") from exc
        except paramiko.AuthenticationException as exc:
            raise HpcError(f"The cluster refused the key for {self.username}. Check the line in ~/.ssh/authorized_keys.") from exc
        except (paramiko.SSHException, OSError) as exc:
            raise HpcError(f"Cannot reach the login node {self.host}:{self.port}: {exc}") from exc
        self.client = client
        return client

    def exec(self, command: str, stdin: bytes | None = None, max_output: int = 0) -> tuple[int, bytes]:
        client = self._connect()
        try:
            channel = client.get_transport().open_session(timeout=self.timeout)
            channel.settimeout(max(self.timeout, 120))
            channel.exec_command(command)
            if stdin:
                channel.sendall(stdin)
            channel.shutdown_write()
            output = bytearray()
            while chunk := channel.recv(1024 * 1024):
                output += chunk
                if max_output and len(output) > max_output:
                    channel.close()
                    raise HpcError("The cluster sent more data than allowed.")
            return channel.recv_exit_status(), bytes(output)
        except HpcError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise transport failures
            self.close()
            raise HpcError(f"SSH request failed: {exc}") from exc

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


@dataclass
class RunnerClient:
    """Typed calls to ``dagonweb-runner`` for one linked user."""

    transport: Transport
    username: str
    min_uid: int = 1000

    def __enter__(self) -> "RunnerClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.transport.close()

    def call(self, *words: str, stdin: bytes | None = None) -> dict[str, Any]:
        code, output = self.transport.exec(shlex.join(words), stdin, max_output=64 * 1024 * 1024)
        try:
            result = json.loads(output.decode("utf-8").strip().splitlines()[-1])
        except (UnicodeDecodeError, ValueError, IndexError):
            raise HpcError(f"Unexpected answer from dagonweb-runner (exit {code}): {output[:300]!r}") from None
        if code or not isinstance(result, dict) or not result.get("ok"):
            raise HpcError(str(result.get("error") if isinstance(result, dict) else result) or f"dagonweb-runner exited with {code}")
        return result

    def ping(self) -> dict[str, Any]:
        info = self.call("ping")
        # Defence in depth: the key must land in the claimed account.
        if info.get("user") != self.username:
            raise HpcError(f"The key signed in as {info.get('user')!r}, not {self.username!r}. Remove the line from that account's authorized_keys.")
        if int(info.get("uid", -1)) < self.min_uid:
            raise HpcError("System accounts cannot be linked.")
        return info

    def submit(self, run_id: int, spec: dict[str, Any], resume: bool = False) -> dict[str, Any]:
        words = ["submit", str(run_id)] + (["--resume"] if resume else [])
        return self.call(*words, stdin=json.dumps(spec).encode("utf-8"))

    def status(self, run_id: int, offset: int) -> dict[str, Any]:
        return self.call("status", str(run_id), str(offset))

    def cancel(self, run_id: int) -> dict[str, Any]:
        return self.call("cancel", str(run_id))

    def ls(self, run_id: int, path: str) -> list[dict[str, Any]]:
        return list(self.call("ls", str(run_id), path or ".").get("files") or [])

    def get(self, run_id: int, path: str, max_bytes: int) -> bytes:
        return self._raw(["get", str(run_id), path, str(max_bytes)], None, max_bytes + 4096)

    def _raw(self, words: list[str], stdin: bytes | None, max_output: int) -> bytes:
        code, output = self.transport.exec(shlex.join(words), stdin, max_output=max_output)
        if code:
            try:
                message = json.loads(output.decode("utf-8").strip().splitlines()[-1]).get("error")
            except (UnicodeDecodeError, ValueError, IndexError, AttributeError):
                message = f"dagonweb-runner exited with {code}"
            raise HpcError(str(message))
        return output

    def archive(self, run_id: int, path: str, max_bytes: int) -> bytes:
        return self._raw(["archive", str(run_id), path or ".", str(max_bytes)], None, max_bytes + 1024 * 1024)

    def put(self, run_id: int, path: str, content: bytes) -> dict[str, Any]:
        return self.call("put", str(run_id), path, stdin=content)

    def info(self) -> dict[str, Any]:
        return self.call("info")

    def keys(self) -> list[dict[str, str]]:
        return [item for item in self.call("keys").get("keys") or [] if isinstance(item, dict)]

    def delete(self, run_id: int) -> None:
        self.call("delete", str(run_id))


TransportFactory = Callable[[User, str], Transport]


def ssh_transport(user: User, private_key: str) -> Transport:
    return SSHTransport(
        str(settings.get("hpc_ssh_host")), int(settings.get("hpc_ssh_port")), str(user.unix_username),
        private_key, str(settings.get("hpc_ssh_host_key") or ""),
    )


def client_for(user: User | None, *, require_linked: bool = True) -> RunnerClient:
    """Return a runner client that signs in as ``user``'s own Unix account."""
    if user is None or not user.unix_username or not user.unix_private_key:
        raise HpcError("Link your cluster account on the account page first.")
    if require_linked and not user.unix_linked:
        raise HpcError("Your cluster account is not verified yet. Verify it on the account page.")
    factory: TransportFactory = current_app.config.get("HPC_TRANSPORT_FACTORY") or ssh_transport
    transport = factory(user, decrypt_private_key(user.unix_private_key))
    return RunnerClient(transport, str(user.unix_username), int(settings.get("hpc_min_uid")))


def enabled() -> bool:
    return bool(settings.get("hpc_enabled"))
