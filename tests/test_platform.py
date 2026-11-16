"""Administration, authorization, SSO, dispatch, assistant, interactive tasks and Python round-trip."""
from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from app import create_app, seed_defaults, settings
from app.config import TestConfig
from app.extensions import db
from app.models import AuditLog, Role, Setting, User, Workflow, WorkflowRun
from app.pycode import PythonImportError, import_python_files, python_bundle, read_upload, workflow_python_source
from app.task_specs import TASK_SPECS, config_from_arguments, dagon_task_arguments
from app.workflows.routes import apply_workflow_document, apply_workflow_template


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


def make_user(email: str, roles: list[str]) -> User:
    user = User(email=email, name=email, active=True, roles=Role.query.filter(Role.name.in_(roles)).all())
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def new_workflow(name: str, template: str | None = None, owner: str = "admin@example.org") -> Workflow:
    user = User.query.filter_by(email=owner).one()
    workflow = Workflow(owner_id=user.id, name=name)
    db.session.add(workflow)
    db.session.flush()
    if template:
        apply_workflow_template(workflow, template)
    db.session.commit()
    return workflow


# ---------------------------------------------------------------- settings & admin


def test_settings_registry_types_secrets_and_env_override(app, monkeypatch):
    with app.app_context():
        assert settings.get("max_concurrent_runs") == 4
        assert settings.get("llm_enabled") is False
        settings.set_value("max_concurrent_runs", 7)
        db.session.commit()
        assert settings.get("max_concurrent_runs") == 7
        monkeypatch.setenv("DAGONWEB_MAX_CONCURRENT_RUNS", "9")
        assert settings.get("max_concurrent_runs") == 9
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert settings.get("llm_api_key") == "sk-test"
        settings.set_value("registration_domains", "uniparthenope.it")
        db.session.commit()
        assert settings.domain_allowed("someone@studenti.uniparthenope.it")
        assert not settings.domain_allowed("someone@example.org")


def test_admin_sections_render_and_validate(app):
    client = login(app)
    for section in ("appearance", "authentication", "execution", "storage", "smtp", "llm"):
        response = client.get(f"/admin/settings/{section}")
        assert response.status_code == 200, section
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/logs").status_code == 200
    bad = client.post("/admin/settings/appearance", data={"site_name": "X", "brand_color": "blue", "navbar_color": "#000000", "default_theme": "auto"})
    assert bad.status_code == 200 and b"#rrggbb" in bad.data
    ok = client.post("/admin/settings/appearance", data={"site_name": "Parthenope Workflows", "tagline": "HPC for all", "brand_color": "#123456", "navbar_color": "#000000", "default_theme": "dark"})
    assert ok.status_code == 302
    page = client.get("/workflows/")
    assert b"Parthenope Workflows" in page.data and b"#123456" in page.data and b'data-default-theme="dark"' in page.data


def test_secret_settings_are_kept_when_left_blank_and_never_rendered(app):
    client = login(app)
    data = {"llm_enabled": "true", "llm_base_url": "https://api.openai.com/v1", "llm_api_key": "sk-secret-value", "llm_model": "gpt-4o-mini", "llm_temperature": "0.2", "llm_timeout": "60", "llm_max_steps": "8"}
    assert client.post("/admin/settings/llm", data=data).status_code == 302
    data["llm_api_key"] = ""
    assert client.post("/admin/settings/llm", data=data).status_code == 302
    with app.app_context():
        assert settings.get("llm_api_key") == "sk-secret-value"
    assert b"sk-secret-value" not in client.get("/admin/settings/llm").data
    data["llm_api_key__clear"] = "1"
    client.post("/admin/settings/llm", data=data)
    with app.app_context():
        assert settings.get("llm_api_key") == ""


def test_logo_upload_validates_content_and_is_sandboxed(app):
    client = login(app)
    fake = client.post("/admin/settings/appearance/logo", data={"logo": (io.BytesIO(b"not a png"), "logo.png")}, content_type="multipart/form-data")
    assert fake.status_code == 302
    with app.app_context():
        assert Setting.query.filter_by(key="logo_file").first() is None
    script = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    client.post("/admin/settings/appearance/logo", data={"logo": (io.BytesIO(script), "logo.svg")}, content_type="multipart/form-data")
    with app.app_context():
        assert Setting.query.filter_by(key="logo_file").first() is None
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    client.post("/admin/settings/appearance/logo", data={"logo": (io.BytesIO(png), "logo.png")}, content_type="multipart/form-data")
    response = client.get("/branding/logo")
    assert response.status_code == 200
    assert "sandbox" in response.headers["Content-Security-Policy"]


def test_role_permissions_gate_routes(app):
    with app.app_context():
        viewer = Role(name="viewer", description="Read only")
        viewer.permissions = []
        db.session.add(viewer)
        db.session.commit()
        make_user("viewer@example.org", ["viewer"])
        workflow_id = new_workflow("Shared", "batch").id
        db.session.get(Workflow, workflow_id).is_public = True
        db.session.commit()
    client = login(app, "viewer@example.org")
    assert client.get("/workflows/").status_code == 200
    assert client.get("/workflows/new").status_code == 403
    assert client.post(f"/workflows/{workflow_id}/run").status_code == 403
    assert client.get("/admin/").status_code == 403
    with app.app_context():
        user = User.query.filter_by(email="viewer@example.org").one()
        assert not user.can("workflows.run")
        assert User.query.filter_by(email="admin@example.org").one().can("workflows.run")


def test_last_admin_cannot_be_demoted(app):
    client = login(app)
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.org").one()
        user_role = Role.query.filter_by(name="user").one()
        url, role_id = f"/admin/users/{admin.id}/edit", user_role.id
    client.post(url, data={"name": "Admin", "email": "admin@example.org", "active": "y", "roles": [str(role_id)]})
    with app.app_context():
        assert User.query.filter_by(email="admin@example.org").one().is_admin


def test_local_login_toggle_and_audit(app):
    with app.app_context():
        make_user("user@example.org", ["user"])
        settings.set_value("local_login_enabled", False)
        db.session.commit()
    client = app.test_client()
    client.post("/login", data={"email": "user@example.org", "password": "password123"})
    with client.session_transaction() as session:
        assert "_user_id" not in session
    client.post("/login", data={"email": "admin@example.org", "password": "admin12345"})
    with client.session_transaction() as session:
        assert "_user_id" in session
    with app.app_context():
        actions = [entry.action for entry in AuditLog.query.all()]
    assert "auth.login_failed" in actions and "auth.login" in actions


def test_open_redirect_after_login_is_blocked(app):
    client = app.test_client()
    response = client.post("/login?next=https://evil.example/", data={"email": "admin@example.org", "password": "admin12345"})
    assert response.headers["Location"].endswith("/workflows/")


# ---------------------------------------------------------------- SSO


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.ok = payload, status, status < 400

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


def _id_token(claims: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def configure_oidc(app):
    with app.app_context():
        for key, value in {"oidc_enabled": True, "oidc_client_id": "client-1", "oidc_client_secret": "s", "oidc_discovery_url": "https://idp.example/.well-known/openid-configuration", "oidc_name": "University SSO"}.items():
            settings.set_value(key, value)
        db.session.commit()


def sso_round_trip(app, monkeypatch, claims_override: dict | None = None):
    import app.auth.sso as sso

    discovery = {"issuer": "https://idp.example", "authorization_endpoint": "https://idp.example/auth", "token_endpoint": "https://idp.example/token"}
    captured: dict = {}
    monkeypatch.setattr(sso.requests, "get", lambda url, **kw: FakeResponse(discovery))

    def token(url, data, **kw):
        claims = {"iss": "https://idp.example", "aud": "client-1", "sub": "abc-123", "email": "new.person@uniparthenope.it", "email_verified": True, "name": "New Person", "exp": time.time() + 300, "nonce": captured["nonce"]}
        claims.update(claims_override or {})
        return FakeResponse({"id_token": _id_token(claims), "access_token": "t"})

    monkeypatch.setattr(sso.requests, "post", token)
    client = app.test_client()
    start = client.get("/login/sso/oidc")
    assert start.status_code == 302 and start.headers["Location"].startswith("https://idp.example/auth?")
    with client.session_transaction() as session:
        captured.update(session["sso"])
        state = session["sso"]["state"]
    return client, client.get(f"/login/sso/oidc/callback?code=xyz&state={state}")


def test_oidc_sign_in_creates_user_with_default_role(app, monkeypatch):
    configure_oidc(app)
    assert b"University SSO" in app.test_client().get("/login").data
    _client, response = sso_round_trip(app, monkeypatch)
    assert response.status_code == 302 and response.headers["Location"].endswith("/workflows/")
    with app.app_context():
        user = User.query.filter_by(email="new.person@uniparthenope.it").one()
        assert user.auth_provider == "oidc" and user.external_id == "abc-123"
        assert [role.name for role in user.roles] == ["user"]


def test_oidc_rejects_bad_state_nonce_and_unverified_takeover(app, monkeypatch):
    configure_oidc(app)
    _client, response = sso_round_trip(app, monkeypatch, {"nonce": "wrong"})
    with app.app_context():
        assert User.query.filter_by(email="new.person@uniparthenope.it").first() is None
    # An unverified email that matches an existing local account is not linked.
    _client, response = sso_round_trip(app, monkeypatch, {"email": "admin@example.org", "email_verified": False, "sub": "attacker"})
    with app.app_context():
        assert User.query.filter_by(email="admin@example.org").one().external_id is None
    client = app.test_client()
    client.get("/login/sso/oidc")
    assert client.get("/login/sso/oidc/callback?code=x&state=forged").status_code == 302


# ---------------------------------------------------------------- dispatch


def test_redis_backend_enqueues_runs(app, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    connection = fakeredis.FakeRedis()
    app.extensions["dagonweb_redis_override"] = connection
    with app.app_context():
        settings.set_value("execution_backend", "redis")
        db.session.commit()
        workflow_id = new_workflow("Queued", "batch").id
    response = login(app).post(f"/workflows/{workflow_id}/run")
    assert response.status_code == 302
    from rq import Queue

    queue = Queue("dagonweb", connection=connection)
    assert queue.count == 1
    job = queue.jobs[0]
    assert job.func_name == "app.jobs.run_workflow_job" and job.args[0] == workflow_id
    with app.app_context():
        run = WorkflowRun.query.one()
        assert run.status == "pending" and "Queued on Redis" in run.log
        from app.executor.dispatch import backend_status

        status = backend_status()
        assert status["redis"]["ok"] and status["redis"]["queued"] == 1


def test_unreachable_redis_falls_back_to_threads(app, monkeypatch):
    from app.executor import dispatch

    started = []
    monkeypatch.setattr(dispatch, "start_thread", lambda app_, workflow_id, run_id, resume=False: started.append(run_id))
    with app.app_context():
        settings.set_value("execution_backend", "redis")
        settings.set_value("redis_url", "redis://127.0.0.1:1/0")
        db.session.commit()
        workflow_id = new_workflow("Fallback", "batch").id
    login(app).post(f"/workflows/{workflow_id}/run")
    with app.app_context():
        run = WorkflowRun.query.one()
        assert started == [run.id] and "Redis queue unavailable" in run.log


# ---------------------------------------------------------------- assistant


def fake_llm(script: list[dict]):
    calls = []

    def transport(payload):
        calls.append(payload)
        return {"choices": [{"message": script[min(len(calls) - 1, len(script) - 1)]}]}

    return transport, calls


def tool_call(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def test_assistant_designs_validates_and_returns_a_draft(app):
    tasks = [
        {"name": "measure", "task_type": "batch", "config": {"command": "mkdir -p outputs\nprintf 'a,b\\n1,2\\n' > outputs/m.csv", "inputs": {}}},
        {"name": "show", "task_type": "table", "config": {"title": "M", "inputs": {"source": "workflow:///measure/outputs/m.csv"}}},
    ]
    transport, calls = fake_llm([tool_call("set_workflow", {"tasks": tasks}), tool_call("validate_workflow", {}, "c2"), {"role": "assistant", "content": "Two tasks: measure then a table."}])
    app.extensions["dagonweb_llm_transport"] = transport
    with app.app_context():
        settings.set_value("llm_enabled", True)
        db.session.commit()
        workflow_id = new_workflow("Assisted").id
    client = login(app)
    response = client.post(f"/assistant/workflows/{workflow_id}/chat", json={"messages": [{"role": "user", "content": "Make a table of measurements"}]})
    assert response.status_code == 200, response.json
    result = response.json
    assert result["changed"] and result["reply"].startswith("Two tasks")
    assert [step["tool"] for step in result["trace"]] == ["set_workflow", "validate_workflow"]
    assert result["check"]["ok"], result["check"]
    assert set(result["draft"]["tasks"]) == {"measure", "show"}
    assert calls[0]["tools"] and calls[0]["messages"][0]["role"] == "system"
    # Nothing is saved until the user applies the draft.
    with app.app_context():
        assert db.session.get(Workflow, workflow_id).tasks == []
    saved = client.post(f"/workflows/{workflow_id}/graph", json=result["draft"])
    assert saved.status_code == 200
    with app.app_context():
        workflow = db.session.get(Workflow, workflow_id)
        assert {task.normalized_task_type for task in workflow.tasks} == {"batch", "table"}
        assert workflow.as_json()["tasks"]["show"]["type"] == "batch"


def test_assistant_tool_errors_are_reported_and_draft_untouched(app):
    transport, _ = fake_llm([tool_call("set_workflow", {"tasks": [{"name": "a", "task_type": "batch", "config": {"command": "cat workflow:///ghost/x"}}]}), {"role": "assistant", "content": "Fixed nothing."}])
    app.extensions["dagonweb_llm_transport"] = transport
    with app.app_context():
        settings.set_value("llm_enabled", True)
        db.session.commit()
        workflow_id = new_workflow("Broken").id
    result = login(app).post(f"/assistant/workflows/{workflow_id}/chat", json={"messages": [{"role": "user", "content": "go"}]}).json
    assert result["trace"][0]["ok"] is False and "ghost" in result["trace"][0]["summary"]
    assert result["changed"] is False


def test_assistant_requires_configuration_and_permission(app):
    with app.app_context():
        workflow_id = new_workflow("Off").id
        role = Role.query.filter_by(name="user").one()
        role.permissions = ["workflows.create", "workflows.run"]
        db.session.commit()
        make_user("plain@example.org", ["user"])
    assert login(app).post(f"/assistant/workflows/{workflow_id}/chat", json={"messages": []}).status_code == 503
    assert login(app, "plain@example.org").post(f"/assistant/workflows/{workflow_id}/chat", json={"messages": []}).status_code == 403


def test_dry_run_validation_finds_shell_syntax_errors(app):
    with app.app_context():
        workflow_id = new_workflow("Dry", "batch").id
    client = login(app)
    assert client.post(f"/assistant/workflows/{workflow_id}/validate", json={}).json["ok"]
    draft = {"name": "Dry", "tasks": {"a": {"name": "a", "type": "batch", "dagonweb": {"config": {"command": "if then fi (", "inputs": {}}}}}}
    result = client.post(f"/assistant/workflows/{workflow_id}/validate", json={"draft": draft}).json
    assert not result["ok"] and "syntax" in result["problems"][0]


# ---------------------------------------------------------------- interactive tasks


def test_interactive_documents_load_in_dagonstar(app):
    from dagon import Workflow as DagonStarWorkflow

    with app.app_context():
        workflow = new_workflow("Interactive_doc", "download")
        document = workflow.as_json()
    assert document["tasks"]["make_table"]["type"] == "batch"
    assert document["tasks"]["make_table"]["dagonweb"]["task_type"] == "table"
    config = {"batch": {"scratch_dir_base": "/tmp", "remove_dir": False}, "ftp_pub": {}, "dagon_service": {"use": "False"}}
    loaded = DagonStarWorkflow("Interactive_doc", config=config, jsonload=document)
    assert {task.name for task in loaded.tasks} == set(document["tasks"])
    # Re-importing keeps the interactive kinds.
    with app.app_context():
        copy = new_workflow("Interactive_copy")
        apply_workflow_document(copy, document)
        db.session.commit()
        assert {task.normalized_task_type for task in copy.tasks} == {"batch", "table", "download"}


def test_run_page_collects_form_and_upload_and_shows_results(app):
    import threading

    from app.executor.local import execute_workflow

    with app.app_context():
        workflow = new_workflow("Interactive_run", "upload")
        workflow.dagonweb = {**workflow.dagonweb}
        run = WorkflowRun(workflow_id=workflow.id, user_id=workflow.owner_id, status="pending", scratch_path="", log="")
        db.session.add(run)
        db.session.commit()
        workflow_id, run_id = workflow.id, run.id

    def execute():
        with app.app_context():
            execute_workflow(db.session.get(Workflow, workflow_id), 1, db.session.get(WorkflowRun, run_id))

    worker = threading.Thread(target=execute)
    worker.start()
    client = login(app)
    deadline = time.time() + 60
    pending = []
    while time.time() < deadline:
        pending = client.get(f"/workflows/{workflow_id}/runs/{run_id}/status").json["interactions"]
        if pending:
            break
        time.sleep(0.3)
    assert pending and pending[0]["task"] == "upload_data" and pending[0]["accept"] == ".csv"
    rejected = client.post(f"/workflows/runs/{run_id}/interact/upload_data", data={"files": (io.BytesIO(b"x"), "data.exe")}, content_type="multipart/form-data")
    assert rejected.status_code == 400
    accepted = client.post(f"/workflows/runs/{run_id}/interact/upload_data", data={"files": (io.BytesIO(b"month,value\nJan,3\nFeb,5\n"), "mine.csv")}, content_type="multipart/form-data")
    assert accepted.status_code == 200, accepted.json
    assert client.post(f"/workflows/runs/{run_id}/interact/upload_data", data={"files": (io.BytesIO(b"a"), "again.csv")}, content_type="multipart/form-data").status_code == 409
    worker.join(120)
    status = client.get(f"/workflows/{workflow_id}/runs/{run_id}/status").json
    assert status["status"] == "success", status["log"]
    kinds = {item["kind"]: item for item in status["results"]}
    assert kinds["table"]["data"]["rows"] == [["Jan", "3"], ["Feb", "5"]]
    assert kinds["chart"]["data"]["series"][0]["values"] == [3.0, 5.0]
    assert client.get(kinds["chart"]["svg_url"]).status_code == 200


def test_form_values_are_validated():
    from app.interactive import coerce_form_values, form_env, form_fields

    fields = form_fields({"fields": [{"name": "n", "type": "integer", "required": True}, {"name": "mode", "type": "select", "options": ["a", "b"]}, {"name": "flag", "type": "boolean"}, {"name": "bad name", "type": "text"}]})
    assert [field["name"] for field in fields] == ["n", "mode", "flag"]
    with pytest.raises(ValueError, match="required"):
        coerce_form_values(fields, {})
    with pytest.raises(ValueError, match="one of"):
        coerce_form_values(fields, {"n": "1", "mode": "c"})
    values = coerce_form_values(fields, {"n": "3", "mode": "b", "flag": "on"})
    assert values == {"n": 3, "mode": "b", "flag": True}
    assert "export MODE=b" in form_env(values)


# ---------------------------------------------------------------- Python import/export


@pytest.mark.parametrize("task_type", sorted(key for key, spec in TASK_SPECS.items() if not spec.interactive))
def test_config_from_arguments_inverts_dagon_task_arguments(task_type):
    spec = TASK_SPECS[task_type]
    args, kwargs = dagon_task_arguments(task_type, "t", spec.default_config)
    name, config = config_from_arguments(task_type, args, kwargs)
    assert name == "t"
    assert dagon_task_arguments(task_type, name, config) == (args, kwargs)


@pytest.mark.parametrize("template", ["batch", "checkpoint", "native", "agentic", "faas", "download", "form", "upload", "slurm"])
def test_python_export_import_round_trip(app, template):
    with app.app_context():
        workflow = new_workflow(f"Round_{template}", template)
        workflow.runtime_options = {**workflow.runtime_options, "max_threads": 4}
        db.session.commit()
        source = workflow_python_source(workflow)
        original = workflow.as_json()
        document, warnings = import_python_files({"workflow.py": source.encode()})
        assert document["name"] == workflow.name
        copy = Workflow(owner_id=workflow.owner_id, name=document["name"])
        copy.dagon_ini = document.pop("dagon_ini", {})
        db.session.add(copy)
        db.session.flush()
        apply_workflow_document(copy, document)
        db.session.commit()
        imported = copy.as_json()
        for name, entry in original["tasks"].items():
            assert imported["tasks"][name]["type"] == entry["type"]
            assert imported["tasks"][name]["dagonweb"]["config"] == entry["dagonweb"]["config"], name
            assert (imported["tasks"][name]["dagonweb"]["x"], imported["tasks"][name]["dagonweb"]["y"]) == (entry["dagonweb"]["x"], entry["dagonweb"]["y"])
            assert sorted(imported["tasks"][name]["prevs"]) == sorted(entry["prevs"])
        assert copy.runtime_options["max_threads"] == 4
        # Exporting the imported workflow gives the same program.
        assert workflow_python_source(copy).replace(copy.name, "X") == source.replace(workflow.name, "X")


HAND_WRITTEN = '''"""Rainfall analysis by the hydrology group.

Downloads nothing: generates data, aggregates it and plots it.
"""
from dagon import Workflow
from dagon.task import DagonTask, TaskType

SCRATCH = "/tmp/rain"
GENERATE = "mkdir -p out; seq 1 10 > out/values.txt"

if __name__ == "__main__":
    workflow = Workflow("Rain-Analysis", portable_emulation=True)

    # Produce synthetic rainfall values.
    # One value per line.
    generate = DagonTask(TaskType.BATCH, "generate", GENERATE)
    total = DagonTask(TaskType.BATCH, "total", f"awk '{{s+=$1}} END {{print s}}' workflow:///generate/out/values.txt > total.txt")
    stats = DagonTask(TaskType.NATIVE, "stats", "analysis.stats:summarize", inputs={"values": "workflow:///generate/out/values.txt"}, outputs={"summary": "summary.json"})
    report = DagonTask(TaskType.DOCKER, "report", "cat workflow:///total/total.txt > report.txt", "python:3.12-slim")
    report.add_dependency_to(stats)

    for task in (generate, total, stats, report):
        workflow.add_task(task)
    workflow.make_dependencies()
    workflow.run()
'''

ANALYSIS = '''import json


def summarize(values):
    numbers = [float(line) for line in open(values) if line.strip()]
    json.dump({"n": len(numbers)}, open("summary.json", "w"))
'''


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_hand_written_dagonstar_zip_imports_and_exports_working_code(app, tmp_path):
    upload = make_zip({"rain/main.py": HAND_WRITTEN, "rain/analysis/stats.py": ANALYSIS, "rain/requirements.txt": "dagonstar\nnumpy\n", "rain/README.md": "x", "../evil.py": "import os", "rain/dagon.ini": "[batch]\nthreads = 2\n[ec2]\nkey = AKIA\n"})
    files = read_upload("rain.zip", upload)
    assert "../evil.py" not in files
    document, warnings = import_python_files(files)
    assert document["name"] == "Rain-Analysis"
    tasks = document["tasks"]
    assert set(tasks) == {"generate", "total", "stats", "report"}
    assert tasks["report"]["type"] == "docker" and tasks["report"]["dagonweb"]["config"]["image"] == "python:3.12-slim"
    assert tasks["report"]["explicit_prevs"] == ["stats"]
    assert tasks["stats"]["dagonweb"]["config"]["source"] == ANALYSIS
    assert tasks["stats"]["dagonweb"]["config"]["requirements"] == "numpy\n"
    assert tasks["total"]["dagonweb"]["config"]["command"].startswith("awk '{s+=$1}")
    assert document["dagonweb"]["runtime"]["portable_emulation"] is True
    assert document["dagonweb"]["python"]["comments"]["generate"] == "Produce synthetic rainfall values.\nOne value per line."
    assert document["dagonweb"]["python"]["docstring"].startswith("Rainfall analysis")
    assert document["dagon_ini"]["ec2"]["key"] == "AKIA"

    client = login(app)
    response = client.post("/workflows/upload", data={"workflow_file": (io.BytesIO(upload), "rain.zip")}, content_type="multipart/form-data")
    assert response.status_code == 302 and "/editor" in response.headers["Location"]
    with app.app_context():
        workflow = Workflow.query.filter_by(name="Rain-Analysis").one()
        assert {(link.source_uid, link.target_uid, link.kind) for link in workflow.links} >= {("generate", "total", "data"), ("stats", "report", "explicit"), ("total", "report", "data")}
        assert "Rainfall analysis" in workflow.description
        source = workflow_python_source(workflow)
        bundle = python_bundle(workflow, None)
    assert "# Produce synthetic rainfall values.\n    # One value per line." in source
    assert "Rainfall analysis by the hydrology group." in source
    compile(source, "rain.py", "exec")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = set(archive.namelist())
        assert {"Rain-Analysis/Rain-Analysis.py", "Rain-Analysis/Rain-Analysis.json", "Rain-Analysis/dagon.ini", "Rain-Analysis/requirements.txt", "Rain-Analysis/README.md", "Rain-Analysis/run.sh"} <= names
        assert "AKIA" not in archive.read("Rain-Analysis/dagon.ini").decode()
        assert "numpy" in archive.read("Rain-Analysis/requirements.txt").decode()
    # The bundle imports back to the same workflow.
    again, _ = import_python_files(read_upload("bundle.zip", bundle))
    assert set(again["tasks"]) == set(tasks)


def test_generated_program_from_imported_code_runs(app, tmp_path):
    import subprocess
    import sys

    program = '''from dagon import Workflow
from dagon.task import DagonTask, TaskType

workflow = Workflow("Tiny")
# First step
a = DagonTask(TaskType.BATCH, "a", "echo 21 > n.txt")
b = DagonTask(TaskType.BATCH, "b", "echo $(( $(cat workflow:///a/n.txt) * 2 )) > answer.txt")
workflow.add_task(a)
workflow.add_task(b)
workflow.make_dependencies()
workflow.run()
'''
    document, _ = import_python_files({"tiny.py": program.encode()})
    with app.app_context():
        workflow = new_workflow("Tiny")
        apply_workflow_document(workflow, document)
        workflow.dagonweb = document["dagonweb"]
        db.session.commit()
        source = workflow_python_source(workflow)
    assert "# First step" in source
    path = tmp_path / "Tiny.py"
    path.write_text(source, encoding="utf-8")
    completed = subprocess.run([sys.executable, str(path)], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr[-2000:]
    answers = list((tmp_path / "scratch").glob("*b/answer.txt"))
    assert answers and answers[0].read_text().strip() == "42"


def test_python_import_rejects_non_literal_arguments_and_never_executes(tmp_path):
    marker = tmp_path / "executed"
    program = f'''import pathlib
pathlib.Path({str(marker)!r}).touch()
from dagon import Workflow
from dagon.task import DagonTask, TaskType
w = Workflow("Dyn")
t = DagonTask(TaskType.BATCH, "t", compute_command())
'''
    with pytest.raises(PythonImportError, match="literals"):
        import_python_files({"dyn.py": program.encode()})
    assert not marker.exists()
    with pytest.raises(PythonImportError):
        import_python_files({"empty.py": b"print('hello')\n"})
    with pytest.raises(PythonImportError):
        read_upload("x.zip", b"not a zip")


def test_portable_json_and_bundle_endpoints(app):
    with app.app_context():
        workflow_id = new_workflow("Exports", "chart").id
    client = login(app)
    portable = client.get(f"/workflows/{workflow_id}/download?portable=1").json
    assert "dagonweb" not in portable and all("dagonweb" not in task for task in portable["tasks"].values())
    bundle = client.get(f"/workflows/{workflow_id}/bundle")
    assert bundle.status_code == 200 and bundle.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(bundle.data)) as archive:
        assert "Exports/Exports.cwl" in archive.namelist()


def test_maintenance_cleanup_removes_old_runs(app, tmp_path):
    from datetime import datetime, timedelta, timezone

    from app.maintenance import cleanup_runs

    with app.app_context():
        workflow = new_workflow("Old", "batch")
        run_dir = Path(settings.get("scratch_dir")) / "workflow-x" / "run-1"
        run_dir.mkdir(parents=True)
        old = WorkflowRun(workflow_id=workflow.id, user_id=workflow.owner_id, status="success", scratch_path=str(run_dir), log="")
        fresh = WorkflowRun(workflow_id=workflow.id, user_id=workflow.owner_id, status="success", scratch_path="", log="")
        db.session.add_all([old, fresh])
        db.session.commit()
        old.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=40)
        db.session.commit()
        assert cleanup_runs(30) == 1
        assert WorkflowRun.query.count() == 1 and not run_dir.exists()
