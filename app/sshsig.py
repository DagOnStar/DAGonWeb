"""Verify OpenSSH signatures made with ``ssh-keygen -Y sign`` (the SSHSIG format).

Users with a linked cluster account can sign in by signing a one-time
challenge with any key in their ``authorized_keys``. Only the public key and
the signature reach DAGonWeb; the private key never leaves their computer.

Format (OpenSSH ``PROTOCOL.sshsig``)::

    "SSHSIG" uint32(1) string(publickey) string(namespace) string(reserved)
             string(hash_algorithm) string(signature)

The signed data is ``"SSHSIG" string(namespace) string(reserved)
string(hash_algorithm) string(H(message))``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

MAGIC = b"SSHSIG"
NAMESPACE = "dagonweb-login"
ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
ARMOR_END = "-----END SSH SIGNATURE-----"
MAX_ARMORED = 16_384
HASHES = {"sha512": hashlib.sha512, "sha256": hashlib.sha256}
# Key types accepted for sign-in. ssh-rsa signatures with SHA-1, DSA and
# FIDO (sk-*) keys are refused: sk keys need extra flags/counter checks.
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
ECDSA_HASHES = {"ecdsa-sha2-nistp256": hashes.SHA256, "ecdsa-sha2-nistp384": hashes.SHA384, "ecdsa-sha2-nistp521": hashes.SHA512}
RSA_HASHES = {"rsa-sha2-256": hashes.SHA256, "rsa-sha2-512": hashes.SHA512}
MIN_RSA_BITS = 2048


class SignatureError(ValueError):
    """The signature is malformed, made for something else, or does not verify."""


@dataclass(frozen=True)
class Signature:
    public_key: bytes  # SSH wire-format public key blob
    namespace: str
    hash_algorithm: str
    signature: bytes  # SSH wire-format signature blob

    @property
    def key_type(self) -> str:
        return _Reader(self.public_key).string().decode("ascii", "replace")


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.offset = data, 0

    def take(self, count: int) -> bytes:
        if count < 0 or self.offset + count > len(self.data):
            raise SignatureError("The signature is truncated.")
        chunk = self.data[self.offset:self.offset + count]
        self.offset += count
        return chunk

    def uint32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def string(self) -> bytes:
        return self.take(self.uint32())

    def mpint(self) -> int:
        return int.from_bytes(self.string(), "big", signed=True)

    def done(self) -> bool:
        return self.offset == len(self.data)


def _string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def parse(armored: str) -> Signature:
    """Decode an armored SSHSIG block."""
    text = (armored or "").strip()
    if len(text) > MAX_ARMORED:
        raise SignatureError("The signature is too long.")
    if not text.startswith(ARMOR_BEGIN) or not text.endswith(ARMOR_END):
        raise SignatureError("Paste the whole signature, from -----BEGIN SSH SIGNATURE----- to -----END SSH SIGNATURE-----.")
    body = "".join(text[len(ARMOR_BEGIN):-len(ARMOR_END)].split())
    try:
        blob = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("The signature is not valid base64.") from exc
    reader = _Reader(blob)
    if reader.take(6) != MAGIC:
        raise SignatureError("This is not an SSH signature.")
    if reader.uint32() != 1:
        raise SignatureError("Unsupported SSH signature version.")
    public_key, namespace, _reserved, hash_algorithm, signature = (reader.string() for _ in range(5))
    if not reader.done():
        raise SignatureError("The signature has trailing data.")
    try:
        return Signature(public_key, namespace.decode("utf-8"), hash_algorithm.decode("ascii"), signature)
    except UnicodeDecodeError as exc:
        raise SignatureError("The signature is malformed.") from exc


def key_blob(line: str) -> bytes | None:
    """The wire-format blob of a ``type base64 [comment]`` public key, or None."""
    parts = (line or "").split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
        if _Reader(blob).string().decode("ascii") != parts[0]:
            return None
    except (binascii.Error, ValueError, SignatureError):
        return None
    return blob


def _load_key(blob: bytes):
    key_type = _Reader(blob).string().decode("ascii", "replace")
    if key_type not in KEY_TYPES:
        raise SignatureError(f"Keys of type {key_type} cannot be used to sign in.")
    try:
        return key_type, serialization.load_ssh_public_key(f"{key_type} {base64.b64encode(blob).decode()}".encode())
    except (ValueError, TypeError) as exc:
        raise SignatureError("The public key in the signature is malformed.") from exc


def verify(signature: Signature, message: bytes, namespace: str = NAMESPACE) -> None:
    """Raise :class:`SignatureError` unless ``signature`` signs ``message`` in ``namespace``."""
    if not hmac.compare_digest(signature.namespace.encode(), namespace.encode()):
        raise SignatureError(f"The signature was made for {signature.namespace!r}, not {namespace!r}. Use -n {namespace}.")
    digest = HASHES.get(signature.hash_algorithm)
    if digest is None:
        raise SignatureError("Unsupported signature hash.")
    signed = MAGIC + _string(namespace.encode()) + _string(b"") + _string(signature.hash_algorithm.encode()) + _string(digest(message).digest())
    key_type, key = _load_key(signature.public_key)
    reader = _Reader(signature.signature)
    sig_type, sig_blob = reader.string().decode("ascii", "replace"), reader.string()
    if not reader.done():
        raise SignatureError("The signature has trailing data.")
    try:
        if key_type == "ssh-ed25519" and sig_type == key_type and isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(sig_blob, signed)
        elif key_type in ECDSA_HASHES and sig_type == key_type and isinstance(key, ec.EllipticCurvePublicKey):
            numbers = _Reader(sig_blob)
            r, s = numbers.mpint(), numbers.mpint()
            key.verify(encode_dss_signature(r, s), signed, ec.ECDSA(ECDSA_HASHES[key_type]()))
        elif key_type == "ssh-rsa" and sig_type in RSA_HASHES and isinstance(key, rsa.RSAPublicKey):
            if key.key_size < MIN_RSA_BITS:
                raise SignatureError(f"RSA keys need at least {MIN_RSA_BITS} bits.")
            key.verify(sig_blob, signed, padding.PKCS1v15(), RSA_HASHES[sig_type]())
        else:
            raise SignatureError("Unsupported signature type (ssh-rsa signatures with SHA-1 are refused).")
    except InvalidSignature as exc:
        raise SignatureError("The signature does not match the challenge.") from exc


def usable_authorized_key(options: str) -> bool:
    """Whether an ``authorized_keys`` entry may sign the user in.

    Entries limited to one command (like DAGonWeb's own key), to some source
    addresses, or that are certificate authorities are not full logins, so
    they are not accepted as proof of identity here.
    """
    lowered = (options or "").lower()
    return not any(token in lowered for token in ("command=", "from=", "cert-authority", "principals="))
