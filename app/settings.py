"""Typed registry of administrator-managed settings.

Every setting the administration panel edits is declared once here with its
section, type, default, and whether it is a secret. Values persist in the
``settings`` table; an environment variable ``DAGONWEB_<KEY>`` (upper case)
overrides the stored value, which lets container deployments pin settings
without the panel.

Secrets are never rendered back into forms: submitting an empty secret keeps
the stored value.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from flask import current_app, has_app_context

from .extensions import db
from .models import Setting

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    section: str
    kind: str = "text"  # text, textarea, password, bool, int, number, select, color, url, email
    default: Any = ""
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()
    group: str = ""
    minimum: float | None = None
    maximum: float | None = None
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def secret(self) -> bool:
        return self.kind == "password"

    @property
    def env_name(self) -> str:
        return f"DAGONWEB_{self.key.upper()}"


SECTIONS: list[tuple[str, str, str]] = [
    ("appearance", "Appearance", "Site name, tagline, logo, colors and default theme."),
    ("authentication", "Authentication", "Local accounts, self-registration and single sign-on providers."),
    ("execution", "Execution", "How workflow runs are dispatched: in-process threads or a Redis queue."),
    ("storage", "Storage", "Scratch directory, retention and upload limits."),
    ("smtp", "Email (SMTP)", "Outgoing mail for registration links and notifications."),
    ("llm", "LLM connector", "OpenAI-compatible endpoint used by the agentic workflow assistant."),
    ("privacy", "Privacy (GDPR)", "Data controller, privacy notice, cookie banner and data retention."),
    ("accessibility", "Accessibility", "Accessibility statement and feedback contact."),
]
SECTION_KEYS = {key for key, _, _ in SECTIONS}

_DEFS: list[SettingDef] = [
    # Appearance
    SettingDef("site_name", "Site name", "appearance", default="DAGonWeb"),
    SettingDef("tagline", "Tagline", "appearance", default="Design, run and inspect DAGonStar workflows."),
    SettingDef("footer_text", "Footer text", "appearance", default="", help="Optional institutional footer, shown next to the runtime version."),
    SettingDef("default_theme", "Default theme", "appearance", "select", "auto", options=(("auto", "Follow the device"), ("light", "Light"), ("dark", "Dark")), help="Users can still pick their own theme."),
    SettingDef("brand_color", "Brand color", "appearance", "color", "#2457d6", help="Used for buttons with white text, so it needs a contrast ratio of at least 4.5:1 against white (WCAG 2.1 AA).", attrs={"contrast_with": "#ffffff"}),
    SettingDef("navbar_color", "Navigation bar color", "appearance", "color", "#0f1b33", help="Shown behind white text; needs a contrast ratio of at least 4.5:1 against white.", attrs={"contrast_with": "#ffffff"}),
    # Authentication
    SettingDef("local_login_enabled", "Allow email and password sign-in", "authentication", "bool", True, group="Local accounts", help="Administrators can always sign in locally."),
    SettingDef("registration_enabled", "Allow self-registration", "authentication", "bool", True, group="Local accounts"),
    SettingDef("registration_domains", "Allowed email domains", "authentication", default="", group="Local accounts", help="Comma-separated, for example <code>uniparthenope.it, example.org</code>. Empty allows any domain. Also applies to SSO sign-ups."),
    SettingDef("guest_playground_enabled", "Let visitors try DAGonWeb without an account", "authentication", "bool", True, group="Guest access", help="Visitors can open the templates, edit workflows in their browser and dry-run them. Nothing is executed and nothing is stored on the server."),
    SettingDef("sso_auto_create", "Create accounts on first SSO sign-in", "authentication", "bool", True, group="Single sign-on"),
    SettingDef("google_enabled", "Enable Google", "authentication", "bool", False, group="Google"),
    SettingDef("google_client_id", "Client ID", "authentication", group="Google"),
    SettingDef("google_client_secret", "Client secret", "authentication", "password", group="Google"),
    SettingDef("google_hosted_domain", "Hosted domain (Workspace)", "authentication", group="Google", help="Optional, for example your university domain."),
    SettingDef("microsoft_enabled", "Enable Microsoft", "authentication", "bool", False, group="Microsoft Entra ID"),
    SettingDef("microsoft_tenant", "Tenant", "authentication", default="organizations", group="Microsoft Entra ID", help="Tenant ID or domain; <code>organizations</code> accepts any work or school account."),
    SettingDef("microsoft_client_id", "Client ID", "authentication", group="Microsoft Entra ID"),
    SettingDef("microsoft_client_secret", "Client secret", "authentication", "password", group="Microsoft Entra ID"),
    SettingDef("oidc_enabled", "Enable generic OpenID Connect", "authentication", "bool", False, group="Other SSO (OpenID Connect)", help="For example Keycloak, eduGAIN/SATOSA proxies, Okta or Auth0."),
    SettingDef("oidc_name", "Button label", "authentication", default="Institutional SSO", group="Other SSO (OpenID Connect)"),
    SettingDef("oidc_discovery_url", "Discovery URL", "authentication", "url", group="Other SSO (OpenID Connect)", help="Ends with <code>/.well-known/openid-configuration</code>."),
    SettingDef("oidc_client_id", "Client ID", "authentication", group="Other SSO (OpenID Connect)"),
    SettingDef("oidc_client_secret", "Client secret", "authentication", "password", group="Other SSO (OpenID Connect)"),
    SettingDef("oidc_scopes", "Scopes", "authentication", default="openid email profile", group="Other SSO (OpenID Connect)"),
    # Execution
    SettingDef("execution_backend", "Run dispatcher", "execution", "select", "thread", options=(("thread", "In-process threads (default)"), ("redis", "Redis queue (RQ workers)")), help="With Redis, start one or more workers with <code>flask --app wsgi worker</code>; runs survive web restarts and scale across hosts that share the scratch volume."),
    SettingDef("redis_url", "Redis URL", "execution", "url", "redis://localhost:6379/0"),
    SettingDef("redis_queue", "Queue name", "execution", default="dagonweb"),
    SettingDef("max_concurrent_runs", "Concurrent runs per process", "execution", "int", 4, minimum=1, maximum=256, help="Thread dispatcher only; with Redis, concurrency is the number of workers."),
    SettingDef("run_timeout_hours", "Run timeout (hours)", "execution", "int", 24, minimum=1, maximum=720),
    # Storage
    SettingDef("scratch_dir", "Scratch directory", "storage", default="", help="Mount this path as a persistent volume in containers. Every run, checkpoint and upload stays below it."),
    SettingDef("run_retention_days", "Delete finished runs after (days)", "storage", "int", 0, minimum=0, maximum=3650, help="0 keeps runs forever. Apply with <code>flask --app wsgi cleanup-runs</code> (for example from cron)."),
    SettingDef("max_upload_mb", "Maximum upload size (MB)", "storage", "int", 50, minimum=1, maximum=10240, help="Workflow imports and interactive Upload tasks."),
    # SMTP
    SettingDef("smtp_host", "SMTP host", "smtp"),
    SettingDef("smtp_port", "SMTP port", "smtp", "int", 587, minimum=1, maximum=65535),
    SettingDef("smtp_from", "From address", "smtp", default="noreply@localhost"),
    SettingDef("smtp_user", "SMTP username", "smtp"),
    SettingDef("smtp_password", "SMTP password", "smtp", "password"),
    SettingDef("smtp_tls", "Use STARTTLS", "smtp", "bool", True),
    # LLM
    SettingDef("llm_enabled", "Enable the workflow assistant", "llm", "bool", False),
    SettingDef("llm_base_url", "API base URL", "llm", "url", "https://api.openai.com/v1", help="Any OpenAI-compatible Chat Completions endpoint (OpenAI, Azure OpenAI, vLLM, Ollama <code>http://ollama:11434/v1</code>)."),
    SettingDef("llm_api_key", "API key", "llm", "password", help="Also read from <code>OPENAI_API_KEY</code>."),
    SettingDef("llm_model", "Model", "llm", default="gpt-4o-mini"),
    SettingDef("llm_temperature", "Temperature", "llm", "number", 0.2, minimum=0, maximum=2),
    SettingDef("llm_timeout", "Request timeout (seconds)", "llm", "int", 60, minimum=5, maximum=600),
    SettingDef("llm_max_steps", "Maximum agent steps per message", "llm", "int", 8, minimum=1, maximum=30, help="Each step is one model call; tools run between steps."),
    # Privacy (GDPR)
    SettingDef("controller_name", "Data controller", "privacy", default="", group="Data controller", help="Organization responsible for the processing, for example your university. Empty uses the site name."),
    SettingDef("controller_address", "Postal address", "privacy", "textarea", "", group="Data controller"),
    SettingDef("privacy_contact_email", "Privacy contact email", "privacy", "email", "", group="Data controller", help="Where users send data protection requests."),
    SettingDef("dpo_email", "Data protection officer email", "privacy", "email", "", group="Data controller", help="Optional; required for public bodies (GDPR Art. 37)."),
    SettingDef("supervisory_authority", "Supervisory authority", "privacy", default="", group="Data controller", help="Where users can lodge a complaint, for example <code>Garante per la protezione dei dati personali (www.garanteprivacy.it)</code>."),
    SettingDef("privacy_policy_version", "Privacy notice version", "privacy", default="1", group="Privacy notice", help="Change it when the notice changes materially: signed-in users are asked to read and accept the new version."),
    SettingDef("privacy_policy_extra", "Additional privacy notice text", "privacy", "textarea", "", group="Privacy notice", help="Shown at the end of the privacy notice (plain text)."),
    SettingDef("cookie_banner_enabled", "Show the cookie banner", "privacy", "bool", True, group="Cookies"),
    SettingDef("audit_retention_days", "Delete audit log entries after (days)", "privacy", "int", 365, minimum=0, maximum=3650, group="Retention", help="0 keeps them forever. Applied by <code>flask --app wsgi cleanup-runs</code>."),
    SettingDef("inactive_account_days", "Report accounts inactive for (days)", "privacy", "int", 0, minimum=0, maximum=3650, group="Retention", help="0 disables it. <code>flask --app wsgi cleanup-runs</code> lists them so administrators can contact or remove them."),
    # Accessibility
    SettingDef("accessibility_contact_email", "Accessibility feedback email", "accessibility", "email", "", help="Where users report accessibility problems. Empty uses the privacy contact."),
    SettingDef("accessibility_enforcement", "Enforcement procedure", "accessibility", "textarea", "", help="Who users can turn to if the answer is unsatisfactory, for example <code>AgID – https://form.agid.gov.it</code> in Italy."),
    SettingDef("accessibility_statement_date", "Statement last reviewed", "accessibility", default="", help="Date of the last review, for example 2026-11-18."),
]
DEFINITIONS: dict[str, SettingDef] = {definition.key: definition for definition in _DEFS}
ENV_ALIASES = {"llm_api_key": "OPENAI_API_KEY", "redis_url": "REDIS_URL"}


def definitions(section: str) -> list[SettingDef]:
    return [definition for definition in _DEFS if definition.section == section]


def _default(definition: SettingDef) -> Any:
    if definition.key == "scratch_dir" and has_app_context():
        return current_app.config["SCRATCH_DIR"]
    return definition.default


def _convert(definition: SettingDef, raw: Any) -> Any:
    if definition.kind == "bool":
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}
    if definition.kind == "int":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return _default(definition)
    if definition.kind == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return _default(definition)
    return "" if raw is None else str(raw)


def env_override(key: str) -> str | None:
    definition = DEFINITIONS[key]
    value = os.getenv(definition.env_name)
    if value is None and key in ENV_ALIASES:
        value = os.getenv(ENV_ALIASES[key])
    return value


def get(key: str) -> Any:
    """Return a typed setting value (environment, then database, then default)."""
    definition = DEFINITIONS[key]
    override = env_override(key)
    if override is not None:
        return _convert(definition, override)
    stored = Setting.query.filter_by(key=key).first() if has_app_context() else None
    if stored is None:
        return _default(definition)
    return _convert(definition, stored.value)


def is_overridden(key: str) -> bool:
    return env_override(key) is not None


def set_value(key: str, value: Any) -> None:
    """Stage a setting change in the session (the caller commits)."""
    definition = DEFINITIONS[key]
    if definition.kind == "bool":
        text = "true" if _convert(definition, value) else "false"
    else:
        text = "" if value is None else str(value)
    setting = Setting.query.filter_by(key=key).first()
    if setting:
        setting.value = text
    else:
        db.session.add(Setting(key=key, value=text))


def validate(definition: SettingDef, raw: str | None) -> Any:
    """Validate one submitted form value; raise ``ValueError`` when invalid."""
    if definition.kind == "bool":
        return raw is not None and raw not in ("", "0", "false", "off")
    value = (raw or "").strip()
    if definition.kind in ("int", "number"):
        if value == "":
            return _default(definition)
        try:
            number: float = int(value) if definition.kind == "int" else float(value)
        except ValueError:
            raise ValueError(f"{definition.label} must be a number.") from None
        if definition.minimum is not None and number < definition.minimum:
            raise ValueError(f"{definition.label} must be at least {definition.minimum:g}.")
        if definition.maximum is not None and number > definition.maximum:
            raise ValueError(f"{definition.label} must be at most {definition.maximum:g}.")
        return number
    if definition.kind == "color":
        if not HEX_COLOR.match(value):
            raise ValueError(f"{definition.label} must be a #rrggbb color.")
        against = definition.attrs.get("contrast_with")
        if against and contrast_ratio(value, against) < 4.5:
            raise ValueError(f"{definition.label} has a contrast ratio of {contrast_ratio(value, against):.1f}:1 against {against}; WCAG 2.1 AA needs at least 4.5:1. Choose a darker color.")
        return value.lower()
    if definition.kind == "email" and value and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
        raise ValueError(f"{definition.label} must be an email address.")
    if definition.kind == "select" and value not in {key for key, _ in definition.options}:
        raise ValueError(f"{definition.label} has an unknown value.")
    if definition.kind == "url" and value and not re.match(r"^(https?|rediss?|unix)://", value):
        raise ValueError(f"{definition.label} must be an http(s) or redis URL.")
    if len(value) > 4000:
        raise ValueError(f"{definition.label} is too long.")
    return value


def section_values(section: str) -> list[dict[str, Any]]:
    """Field descriptions for rendering a section form (secrets masked)."""
    rows = []
    for definition in definitions(section):
        value = get(definition.key)
        rows.append({
            "def": definition,
            "value": "" if definition.secret else value,
            "has_secret": bool(value) if definition.secret else False,
            "overridden": is_overridden(definition.key),
        })
    return rows


def domain_allowed(email: str) -> bool:
    """Apply the administrator's allowed-domain list to an email address."""
    domains = [item.strip().lower().lstrip("@") for item in str(get("registration_domains")).split(",") if item.strip()]
    if not domains:
        return True
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in domains)


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 2.1 contrast ratio between two #rrggbb colors."""
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)
