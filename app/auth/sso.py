"""Single sign-on with OpenID Connect (Google, Microsoft Entra ID, generic).

The authorization-code flow uses ``state``, ``nonce`` and PKCE. The ID token
comes straight from the provider's token endpoint over TLS, which OpenID
Connect Core (section 3.1.3.7) accepts in place of a signature check; the
audience, nonce and expiry are still verified.

Accounts are matched by provider and subject. An existing local account is
linked by email only when the provider marks the address as verified, so a
multi-tenant provider cannot take over an account by asserting an email.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from flask import current_app, session

from .. import settings

DISCOVERY_TTL = 3600
HTTP_TIMEOUT = 10


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    discovery_url: str
    client_id: str
    client_secret: str
    scopes: str
    extra_params: dict[str, str]


def providers() -> list[Provider]:
    """Return the SSO providers the administrator enabled and configured."""
    configured: list[Provider] = []
    if settings.get("google_enabled") and settings.get("google_client_id"):
        extra = {"prompt": "select_account"}
        if settings.get("google_hosted_domain"):
            extra["hd"] = str(settings.get("google_hosted_domain"))
        configured.append(Provider("google", "Google", "https://accounts.google.com/.well-known/openid-configuration", str(settings.get("google_client_id")), str(settings.get("google_client_secret")), "openid email profile", extra))
    if settings.get("microsoft_enabled") and settings.get("microsoft_client_id"):
        tenant = str(settings.get("microsoft_tenant") or "organizations").strip() or "organizations"
        configured.append(Provider("microsoft", "Microsoft", f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration", str(settings.get("microsoft_client_id")), str(settings.get("microsoft_client_secret")), "openid email profile", {"prompt": "select_account"}))
    if settings.get("oidc_enabled") and settings.get("oidc_client_id") and settings.get("oidc_discovery_url"):
        configured.append(Provider("oidc", str(settings.get("oidc_name") or "Single sign-on"), str(settings.get("oidc_discovery_url")), str(settings.get("oidc_client_id")), str(settings.get("oidc_client_secret")), str(settings.get("oidc_scopes") or "openid email profile"), {}))
    return configured


def provider(key: str) -> Provider | None:
    return next((item for item in providers() if item.key == key), None)


def discovery(item: Provider) -> dict[str, Any]:
    """Fetch (and cache per process) the provider's OpenID configuration."""
    cache: dict[str, tuple[float, dict[str, Any]]] = current_app.extensions.setdefault("dagonweb_oidc_discovery", {})
    cached = cache.get(item.discovery_url)
    if cached and cached[0] > time.time():
        return cached[1]
    response = requests.get(item.discovery_url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    document = response.json()
    for key in ("authorization_endpoint", "token_endpoint"):
        if not str(document.get(key, "")).startswith("https://"):
            raise ValueError(f"OpenID configuration has no HTTPS {key}.")
    cache[item.discovery_url] = (time.time() + DISCOVERY_TTL, document)
    return document


def authorization_url(item: Provider, redirect_uri: str) -> str:
    """Start the flow: remember state, nonce and PKCE verifier in the session."""
    config = discovery(item)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session["sso"] = {"provider": item.key, "state": state, "nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri, "started": time.time()}
    params = {
        "response_type": "code",
        "client_id": item.client_id,
        "redirect_uri": redirect_uri,
        "scope": item.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **item.extra_params,
    }
    return f"{config['authorization_endpoint']}?{urlencode(params)}"


def _decode_segment(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()))


def complete(item: Provider, code: str, state: str) -> dict[str, Any]:
    """Exchange the code and return verified identity claims."""
    pending = session.pop("sso", None)
    if not pending or pending.get("provider") != item.key or not secrets.compare_digest(str(pending.get("state", "")), state or ""):
        raise ValueError("The sign-in request expired or was not started here. Please try again.")
    if time.time() - float(pending.get("started", 0)) > 600:
        raise ValueError("The sign-in request expired. Please try again.")
    config = discovery(item)
    response = requests.post(
        config["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "client_id": item.client_id,
            "client_secret": item.client_secret,
            "code_verifier": pending["verifier"],
        },
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        raise ValueError("The identity provider rejected the sign-in.")
    tokens = response.json()
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        raise ValueError("The identity provider returned no ID token.")
    claims = _decode_segment(id_token.split(".")[1])
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if item.client_id not in audiences:
        raise ValueError("The ID token was issued for another application.")
    if claims.get("nonce") != pending["nonce"]:
        raise ValueError("The ID token nonce does not match.")
    if float(claims.get("exp", 0)) < time.time() - 60:
        raise ValueError("The ID token has expired.")
    issuer = config.get("issuer", "")
    if item.key != "microsoft" and issuer and claims.get("iss") != issuer:
        # Microsoft's multi-tenant metadata uses a ``{tenantid}`` template.
        raise ValueError("The ID token issuer does not match.")
    if not claims.get("email") and tokens.get("access_token") and config.get("userinfo_endpoint"):
        info = requests.get(config["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=HTTP_TIMEOUT)
        if info.ok:
            claims = {**info.json(), **claims}
    email = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if "@" not in email:
        raise ValueError("The identity provider did not share an email address.")
    if item.key == "google" and settings.get("google_hosted_domain") and claims.get("hd") != settings.get("google_hosted_domain"):
        raise ValueError("This Google account is not part of the allowed domain.")
    verified = claims.get("email_verified") in (True, "true")
    return {"subject": str(claims.get("sub") or ""), "email": email, "name": str(claims.get("name") or email), "email_verified": verified}
