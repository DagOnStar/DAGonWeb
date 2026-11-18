"""Guest playground, cookie consent, GDPR rights and accessibility checks."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app import create_app, privacy, seed_defaults, settings
from app.config import TestConfig
from app.extensions import db
from app.models import AuditLog, RegistrationToken, Role, Setting, User, Workflow, WorkflowRun
from app.workflows.routes import apply_workflow_template


@pytest.fixture()
def app(tmp_path):
    application = create_app(TestConfig)
    application.config["DAGON_INI_PATH"] = str(tmp_path / "dagon.ini")
    application.instance_path = str(tmp_path / "instance")
    with application.app_context():
        db.create_all()
        seed_defaults(application)
        Setting.query.filter_by(key="scratch_dir").one().value = str(tmp_path / "scratch")
        db.session.commit()
    return application


def login(app, email: str = "admin@example.org"):
    client = app.test_client()
    with app.app_context():
        user_id = str(User.query.filter_by(email=email).one().id)
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


def make_user(email: str, roles: list[str] = ("user",), password: str = "correct horse battery") -> User:
    user = User(email=email, name=email.split("@")[0], active=True, roles=Role.query.filter(Role.name.in_(list(roles))).all())
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- guest playground


def test_anonymous_visitors_land_on_the_playground(app):
    client = app.test_client()
    assert client.get("/").headers["Location"].endswith("/try/")
    page = client.get("/try/").get_data(as_text=True)
    assert "No account needed" in page and "/try/batch" in page
    editor = client.get("/try/batch").get_data(as_text=True)
    assert 'data-mode="guest"' in editor and "Dry run" in editor and "Create a free account" in editor


def test_playground_can_be_disabled(app):
    with app.app_context():
        settings.set_value("guest_playground_enabled", False)
        db.session.commit()
    client = app.test_client()
    assert client.get("/").headers["Location"].endswith("/login")
    assert client.get("/try/").status_code == 404
    assert client.post("/try/dry-run", json={"tasks": {}}).status_code == 404


def test_guest_dry_run_plans_without_executing_or_storing(app):
    client = app.test_client()
    document = client.get("/try/batch.json").get_json()
    result = client.post("/try/dry-run", json=document).get_json()
    assert result["ok"] is True
    assert result["stages"] == [["extract"], ["transform_a", "transform_b"], ["merge"]]
    assert result["tasks"]["merge"]["after"] == ["transform_a", "transform_b"]
    assert "DAGonStar accepted 4 tasks" in result["dagonstar"]
    with app.app_context():
        assert Workflow.query.count() == 0 and WorkflowRun.query.count() == 0 and AuditLog.query.count() == 0
        assert not any(Path(str(settings.get("scratch_dir"))).glob("**/*"))


def test_guest_dry_run_reports_problems(app):
    client = app.test_client()
    document = {"name": "broken", "tasks": {"a": {"name": "a", "type": "batch", "dagonweb": {"config": {"command": "if then fi (", "inputs": {}}}}}}
    result = client.post("/try/dry-run", json=document).get_json()
    assert result["ok"] is False and "shell syntax error" in " ".join(result["problems"])
    cycle = {"name": "cycle", "tasks": {
        "a": {"name": "a", "type": "batch", "explicit_prevs": ["b"], "dagonweb": {"config": {"command": "true"}}},
        "b": {"name": "b", "type": "batch", "explicit_prevs": ["a"], "dagonweb": {"config": {"command": "true"}}},
    }}
    assert client.post("/try/dry-run", json=cycle).get_json()["ok"] is False
    assert client.post("/try/dry-run", json={"nope": 1}).status_code == 400
    many = {"name": "big", "tasks": {f"t{i}": {"name": f"t{i}", "type": "batch", "dagonweb": {"config": {"command": "true"}}} for i in range(61)}}
    assert client.post("/try/dry-run", json=many).status_code == 400


def test_guest_dry_run_never_imports_native_code(app, tmp_path):
    marker = tmp_path / "imported.txt"
    source = f"open({str(marker)!r}, 'w').write('imported')\n\ndef run(output_file):\n    return {{}}\n"
    document = {"name": "native_probe", "tasks": {"probe": {"name": "probe", "type": "native", "dagonweb": {"config": {"callable": "probe_mod:run", "source": source, "inputs": {}, "outputs": {"output_file": "r.json"}}}}}}
    result = app.test_client().post("/try/dry-run", json=document).get_json()
    assert result["ok"] is True
    assert any("not imported" in warning for warning in result["warnings"])
    assert not marker.exists()


# ---------------------------------------------------------------- cookies


def test_cookie_banner_and_policy(app):
    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)
    assert 'id="cookieBanner"' in page and 'data-enabled="1"' in page
    assert "Accept all" in page and "Necessary only" in page and 'id="consentPreferences"' in page
    policy = client.get("/cookies").get_data(as_text=True)
    for item in privacy.STORAGE_ITEMS:
        assert item.name in policy
    with app.app_context():
        settings.set_value("cookie_banner_enabled", False)
        db.session.commit()
    assert 'data-enabled="0"' in client.get("/login").get_data(as_text=True)


def test_session_cookie_is_first_party_and_protected(app):
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["REMEMBER_COOKIE_DURATION"] <= timedelta(days=30)
    response = app.test_client().get("/privacy")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


# ---------------------------------------------------------------- GDPR


def test_privacy_notice_reflects_configuration(app):
    with app.app_context():
        settings.set_value("controller_name", "Università degli Studi di Napoli Parthenope")
        settings.set_value("privacy_contact_email", "privacy@example.org")
        settings.set_value("llm_enabled", True)
        settings.set_value("audit_retention_days", 90)
        db.session.commit()
    page = app.test_client().get("/privacy").get_data(as_text=True)
    assert "Parthenope" in page and "privacy@example.org" in page
    assert "language-model service" in page and "90 days" in page
    assert "Art. 17" in page and "Art. 77" in page


def test_registration_requires_privacy_acknowledgement(app):
    client = app.test_client()
    with app.app_context():
        record, token = RegistrationToken.issue("new@example.org", now() + timedelta(hours=1))
        db.session.add(record)
        db.session.commit()
    form = {"token": token, "password": "a long password", "confirm": "a long password", "name": "New Person"}
    refused = client.post(f"/register/complete?token={token}", data=form)
    assert refused.status_code == 200 and "accept the privacy notice" in refused.get_data(as_text=True)
    accepted = client.post(f"/register/complete?token={token}", data={**form, "accept_privacy": "y"})
    assert accepted.status_code == 302
    with app.app_context():
        user = User.query.filter_by(email="new@example.org").one()
        assert user.name == "New Person"
        assert user.privacy_version == "1" and user.privacy_accepted_at is not None


def test_signed_in_users_accept_a_new_privacy_notice(app):
    app.config["PRIVACY_ACCEPTANCE_REQUIRED"] = True
    with app.app_context():
        user = make_user("reader@example.org")
        privacy.accept_policy(user)
        db.session.commit()
    client = login(app, "reader@example.org")
    assert client.get("/workflows/", headers={"Accept": "text/html"}).status_code == 200
    with app.app_context():
        settings.set_value("privacy_policy_version", "2")
        db.session.commit()
    redirect = client.get("/workflows/", headers={"Accept": "text/html"})
    assert redirect.status_code == 302 and "/privacy/accept" in redirect.headers["Location"]
    assert client.get("/workflows/", headers={"Accept": "application/json"}).status_code == 200
    assert client.post("/workflows/upload", headers={"Accept": "application/json"}).status_code == 428
    assert client.get("/account/").status_code == 200 and client.get("/privacy").status_code == 200
    assert client.post("/privacy/accept", data={"next": "/workflows/"}).status_code == 200  # not ticked
    done = client.post("/privacy/accept", data={"accept": "1", "next": "/workflows/"})
    assert done.status_code == 302 and done.headers["Location"].endswith("/workflows/")
    with app.app_context():
        assert User.query.filter_by(email="reader@example.org").one().privacy_version == "2"


def test_account_export_contains_all_personal_data(app):
    with app.app_context():
        user = make_user("owner@example.org")
        workflow = Workflow(owner_id=user.id, name="mine")
        db.session.add(workflow)
        db.session.flush()
        apply_workflow_template(workflow, "batch")
        db.session.add(WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="success", scratch_path="", log="done"))
        db.session.add(AuditLog(user_id=user.id, action="auth.login", target=user.email, ip_address="192.0.2.1"))
        db.session.commit()
    client = login(app, "owner@example.org")
    response = client.get("/account/export")
    assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]
    data = json.loads(response.data)
    assert data["account"]["email"] == "owner@example.org"
    assert data["workflows"][0]["document"]["tasks"]["merge"]
    assert data["runs"][0]["log"] == "done"
    assert data["audit_log"][0]["ip_address"] == "192.0.2.1"


def test_account_erasure_deletes_data_and_detaches_audit(app, tmp_path):
    with app.app_context():
        user = make_user("leaver@example.org")
        workflow = Workflow(owner_id=user.id, name="gone")
        db.session.add(workflow)
        db.session.flush()
        run_dir = Path(str(settings.get("scratch_dir"))) / "run_leaver"
        (run_dir / "task").mkdir(parents=True)
        (run_dir / "task" / "data.txt").write_text("personal")
        db.session.add(WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="success", scratch_path=str(run_dir)))
        db.session.add(AuditLog(user_id=user.id, action="auth.login", target=user.email, ip_address="192.0.2.1"))
        db.session.add(RegistrationToken(email=user.email, token_hash="x" * 64, expires_at=now()))
        db.session.commit()
        user_id = user.id
    client = login(app, "leaver@example.org")
    wrong = client.post("/account/delete", data={"delete-confirm_email": "other@example.org", "delete-password": "correct horse battery"})
    assert wrong.status_code == 400
    bad_password = client.post("/account/delete", data={"delete-confirm_email": "leaver@example.org", "delete-password": "nope"})
    assert bad_password.status_code == 400
    done = client.post("/account/delete", data={"delete-confirm_email": "leaver@example.org", "delete-password": "correct horse battery"})
    assert done.status_code == 302
    with app.app_context():
        assert db.session.get(User, user_id) is None
        assert Workflow.query.filter_by(name="gone").count() == 0 and WorkflowRun.query.count() == 0
        assert RegistrationToken.query.count() == 0
        entries = AuditLog.query.all()
        assert all(entry.user_id is None for entry in entries)
        assert not any("leaver@example.org" in entry.target for entry in entries)
        assert any(entry.action == "account.erased" for entry in entries)
        assert not any(entry.ip_address == "192.0.2.1" for entry in entries)
    assert not run_dir.exists()


def test_erasure_refuses_last_admin_and_active_runs(app):
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.org").one()
        with pytest.raises(privacy.ErasureRefused):
            privacy.erase_user(admin)
        db.session.rollback()
        user = make_user("busy@example.org")
        workflow = Workflow(owner_id=user.id, name="busy")
        db.session.add(workflow)
        db.session.flush()
        db.session.add(WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="running", scratch_path=""))
        db.session.commit()
        with pytest.raises(privacy.ErasureRefused):
            privacy.erase_user(user)


def test_admin_can_export_and_erase_a_user(app):
    with app.app_context():
        user = make_user("subject@example.org")
        user_id = user.id
    client = login(app)
    exported = client.get(f"/admin/users/{user_id}/export")
    assert json.loads(exported.data)["account"]["email"] == "subject@example.org"
    assert client.post(f"/admin/users/{user_id}/erase").status_code == 302  # not confirmed
    with app.app_context():
        assert db.session.get(User, user_id) is not None
    client.post(f"/admin/users/{user_id}/erase", data={"confirm": "1"})
    with app.app_context():
        assert db.session.get(User, user_id) is None


def test_retention_purges_audit_log_and_registration_links(app):
    with app.app_context():
        db.session.add(AuditLog(action="old", created_at=now() - timedelta(days=400)))
        db.session.add(AuditLog(action="new"))
        db.session.add(RegistrationToken(email="a@example.org", token_hash="a" * 64, expires_at=now() - timedelta(hours=1)))
        db.session.add(RegistrationToken(email="b@example.org", token_hash="b" * 64, expires_at=now() + timedelta(hours=1)))
        db.session.commit()
        assert privacy.purge_audit_log(365) == 1
        assert [entry.action for entry in AuditLog.query.all()] == ["new"]
        assert privacy.purge_registration_tokens() == 1
        assert RegistrationToken.query.one().email == "b@example.org"
    result = app.test_cli_runner().invoke(args=["cleanup-runs"])
    assert "registration link" in result.output


# ---------------------------------------------------------------- accessibility


def test_brand_colors_must_meet_contrast(app):
    client = login(app)
    response = client.post("/admin/settings/appearance", data={"site_name": "X", "brand_color": "#ffcc00", "navbar_color": "#0f1b33", "default_theme": "auto"})
    assert "contrast ratio" in response.get_data(as_text=True)
    assert settings.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)


class _A11yParser(HTMLParser):
    """Collects what the static accessibility checks need."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = None
        self.title = ""
        self.in_title = False
        self.ids: list[str] = []
        self.labels_for: set[str] = set()
        self.controls: list[dict] = []
        self.images: list[dict] = []
        self.h1 = 0
        self.skip_target = None
        self.label_depth = 0
        self.buttons: list[dict] = []
        self.current_button: dict | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "html":
            self.lang = attributes.get("lang")
        elif tag == "title":
            self.in_title = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "label":
            self.label_depth += 1
            if attributes.get("for"):
                self.labels_for.add(attributes["for"])
        elif tag == "img":
            self.images.append(attributes)
        elif tag == "a" and "skip-link" in (attributes.get("class") or ""):
            self.skip_target = attributes.get("href")
        elif tag in ("input", "select", "textarea") and attributes.get("type") not in ("hidden", "submit", "button", "reset"):
            self.controls.append({**attributes, "_wrapped": self.label_depth > 0})
        elif tag == "button":
            self.current_button = {**attributes, "_text": ""}

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "label":
            self.label_depth = max(0, self.label_depth - 1)
        elif tag == "button" and self.current_button is not None:
            self.buttons.append(self.current_button)
            self.current_button = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.current_button is not None:
            self.current_button["_text"] += data


def assert_accessible(html: str, path: str) -> None:
    parser = _A11yParser()
    parser.feed(html)
    assert parser.lang, f"{path}: <html> needs a lang attribute"
    assert parser.title.strip(), f"{path}: empty <title>"
    assert parser.h1 >= 1, f"{path}: no <h1>"
    assert parser.skip_target == "#main" and "main" in parser.ids, f"{path}: missing skip link to #main"
    duplicates = {item for item in parser.ids if parser.ids.count(item) > 1}
    assert not duplicates, f"{path}: duplicate ids {duplicates}"
    for image in parser.images:
        assert "alt" in image, f"{path}: <img> without alt: {image}"
    for control in parser.controls:
        named = control.get("aria-label") or control.get("aria-labelledby") or control.get("title") or control["_wrapped"] or (control.get("id") in parser.labels_for)
        assert named, f"{path}: unlabelled form control {control}"
    for button in parser.buttons:
        assert button["_text"].strip() or button.get("aria-label") or button.get("title"), f"{path}: button without a name {button}"


ANONYMOUS_PAGES = ["/login", "/register", "/try/", "/try/batch", "/try/empty", "/privacy", "/cookies", "/accessibility"]
ADMIN_PAGES = [
    "/workflows/", "/workflows/runs", "/account/", "/admin/", "/admin/users", "/admin/users/new", "/admin/roles", "/admin/roles/new", "/admin/logs",
    "/admin/dagon-ini", *[f"/admin/settings/{key}" for key, _, _ in settings.SECTIONS],
]


def test_pages_pass_static_accessibility_checks(app):
    anonymous = app.test_client()
    for path in ANONYMOUS_PAGES:
        response = anonymous.get(path)
        assert response.status_code == 200, path
        assert_accessible(response.get_data(as_text=True), path)
    client = login(app)
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.org").one()
        workflow = Workflow(owner_id=admin.id, name="a11y")
        db.session.add(workflow)
        db.session.flush()
        apply_workflow_template(workflow, "chart")
        db.session.commit()
        pages = [*ADMIN_PAGES, f"/workflows/{workflow.id}/editor", f"/workflows/{workflow.id}/setup", f"/admin/users/{admin.id}/edit"]
    for path in pages:
        response = client.get(path)
        assert response.status_code == 200, path
        assert_accessible(response.get_data(as_text=True), path)
