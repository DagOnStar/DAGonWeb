from app import create_app, seed_defaults
from app.config import TestConfig
from app.extensions import db
import pytest

from app.executor.local import execute_task
from app.models import Role, TaskRun, User, Workflow, WorkflowLink, WorkflowRun, WorkflowTask
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


def test_graph_validation_rejects_cycles_and_unknown_tasks():
    with pytest.raises(ValueError, match="acyclic"):
        validate_graph_payload({"tasks": [{"uid": "a"}, {"uid": "b"}], "links": [{"source_uid": "a", "target_uid": "b"}, {"source_uid": "b", "target_uid": "a"}]})
    with pytest.raises(ValueError, match="existing tasks"):
        validate_graph_payload({"tasks": [{"uid": "a"}], "links": [{"source_uid": "a", "target_uid": "missing"}]})


def test_executor_rejects_failed_commands_and_unsafe_input_filename(tmp_path):
    app = make_app()
    with app.app_context():
        workflow = Workflow(id=1, owner_id=1, name="Test")
        bash = WorkflowTask(uid="bash", label="Bash", task_type="bash")
        bash.config = {"command": "exit 7"}
        with pytest.raises(RuntimeError, match="status 7"):
            execute_task(bash, tmp_path, workflow)

        input_task = WorkflowTask(uid="input", label="Input", task_type="input")
        input_task.config = {"filename": "../outside.txt", "value": "nope"}
        with pytest.raises(ValueError, match="Unsafe scratch path"):
            execute_task(input_task, tmp_path, workflow)


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
        db.session.add(TaskRun(workflow_run_id=run.id, task_uid="task", status="success", scratch_path=str(task_dir), log="private"))
        db.session.commit()
        viewer_id = str(viewer.id)
        run_id = run.id
        workflow_id = workflow.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = viewer_id
        session["_fresh"] = True
    assert client.get(f"/workflows/{workflow_id}/runs/{run_id}").status_code == 403
    assert client.get(f"/workflows/files?path={task_dir}").status_code == 403
