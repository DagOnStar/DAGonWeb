import io
import json

from app import create_app, seed_defaults
from app.config import TestConfig
from app.extensions import db
import pytest

from app.executor.local import execute_task, safe_child
from app.models import TASK_TYPE_LABELS, Role, TaskRun, TaskType, User, Workflow, WorkflowLink, WorkflowRun, WorkflowTask
from app.workflows.routes import validate_graph_payload


def make_app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        seed_defaults(app)
    return app


def test_seed_creates_admin_and_roles():
    app = make_app()
    with app.app_context():
        assert Role.query.filter_by(name="admin").first() is not None
        assert Role.query.filter_by(name="user").first() is not None
        assert User.query.filter_by(email="admin@example.org").first() is not None


def test_workflow_link_uri_contract():
    link = WorkflowLink(source_uid="a", source_output="output", target_uid="b", target_input="input", workflow_uri="workflow://a/output")
    assert link.to_dict()["workflow_uri"] == "workflow://a/output"


def test_every_task_type_has_a_palette_label():
    expected_types = {"checkpoint", "batch", "slurm", "cloud", "docker", "llm", "native", "web"}
    assert {task_type.value for task_type in TaskType} == expected_types
    assert set(TASK_TYPE_LABELS) == expected_types


def test_workflow_json_download_and_upload_round_trip():
    app = make_app()
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.org").first()
        workflow = Workflow(owner_id=admin.id, name="Portable workflow", description="Round trip")
        task = WorkflowTask(uid="checkpoint", label="Checkpoint", task_type="checkpoint")
        task.config = {"name": "ready"}
        workflow.tasks.append(task)
        db.session.add(workflow)
        db.session.commit()
        admin_id = str(admin.id)
        workflow_id = workflow.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = admin_id
        session["_fresh"] = True
    downloaded = client.get(f"/workflows/{workflow_id}/download")
    assert downloaded.status_code == 200
    document = json.loads(downloaded.data)
    assert document["format"] == "dagonweb.workflow/v1"
    assert "id" not in document

    document["name"] = "Imported workflow"
    uploaded = client.post("/workflows/upload", data={"workflow_file": (io.BytesIO(json.dumps(document).encode()), "workflow.json")}, content_type="multipart/form-data")
    assert uploaded.status_code == 302
    with app.app_context():
        imported = Workflow.query.filter_by(name="Imported workflow").one()
        assert imported.as_json()["tasks"] == document["tasks"]


def test_graph_validation_rejects_cycles_and_unknown_tasks():
    with pytest.raises(ValueError, match="acyclic"):
        validate_graph_payload({"tasks": [{"uid": "a"}, {"uid": "b"}], "links": [{"source_uid": "a", "target_uid": "b"}, {"source_uid": "b", "target_uid": "a"}]})
    with pytest.raises(ValueError, match="existing tasks"):
        validate_graph_payload({"tasks": [{"uid": "a"}], "links": [{"source_uid": "a", "target_uid": "missing"}]})


def test_executor_rejects_failed_batch_commands_and_unsafe_paths(tmp_path):
    app = make_app()
    with app.app_context():
        workflow = Workflow(id=1, owner_id=1, name="Test")
        batch = WorkflowTask(uid="batch", label="Batch", task_type="batch")
        batch.config = {"command": "exit 7"}
        with pytest.raises(RuntimeError, match="status 7"):
            execute_task(batch, tmp_path, workflow)
        with pytest.raises(ValueError, match="Unsafe scratch path"):
            safe_child(tmp_path, "../outside.txt")


def test_users_cannot_inspect_other_users_run_files(tmp_path):
    app = make_app()
    with app.app_context():
        owner = User(email="owner@example.org", name="Owner", active=True)
        owner.set_password("password123")
        viewer = User(email="viewer@example.org", name="Viewer", active=True)
        viewer.set_password("password123")
        db.session.add_all([owner, viewer])
        db.session.flush()
        workflow = Workflow(owner_id=owner.id, name="Public workflow", is_public=True)
        db.session.add(workflow)
        db.session.flush()
        task_dir = tmp_path / "private-task"
        task_dir.mkdir()
        run = WorkflowRun(workflow_id=workflow.id, user_id=owner.id, status="success", scratch_path=str(tmp_path), log="private")
        db.session.add(run)
        db.session.flush()
        (task_dir / "result.txt").write_text("private result", encoding="utf-8")
        task_run = TaskRun(workflow_run_id=run.id, task_uid="task", status="success", scratch_path=str(task_dir), log="private")
        db.session.add(task_run)
        db.session.commit()
        viewer_id = str(viewer.id)
        owner_id = str(owner.id)
        run_id = run.id
        workflow_id = workflow.id
        task_run_id = task_run.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = viewer_id
        session["_fresh"] = True
    assert client.get(f"/workflows/{workflow_id}/runs/{run_id}").status_code == 403
    assert client.get(f"/workflows/files?path={task_dir}").status_code == 403
    assert client.get(f"/workflows/task-runs/{task_run_id}/files").status_code == 403

    with client.session_transaction() as session:
        session["_user_id"] = owner_id
        session["_fresh"] = True
    response = client.get(f"/workflows/task-runs/{task_run_id}/file?path=result.txt")
    assert response.status_code == 200
    assert response.get_json()["content"] == "private result"
