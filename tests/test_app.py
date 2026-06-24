import io
import json

from app import create_app, seed_defaults
from app.config import TestConfig
from app.extensions import db
import pytest

from app.executor.local import dagonstar_document, execute_task, materialize_native_sources, safe_child
from app.models import TASK_TYPE_LABELS, Role, Setting, TaskRun, TaskType, User, Workflow, WorkflowLink, WorkflowRun, WorkflowTask
from app.workflows.routes import apply_import_layout, validate_graph_payload, workflow_python_source


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
    assert document["name"] == "Portable workflow"
    assert set(document["tasks"]) == {"checkpoint"}

    document["name"] = "Imported workflow"
    uploaded = client.post("/workflows/upload", data={"workflow_file": (io.BytesIO(json.dumps(document).encode()), "workflow.json")}, content_type="multipart/form-data")
    assert uploaded.status_code == 302
    with app.app_context():
        imported = Workflow.query.filter_by(name="Imported workflow").one()
        imported_task = imported.as_json()["tasks"]["checkpoint"]
        assert imported_task["type"] == document["tasks"]["checkpoint"]["type"]
        assert imported_task["dagonweb"]["config"] == document["tasks"]["checkpoint"]["dagonweb"]["config"]


def test_web_task_serializes_native_dagonstar_specification():
    app = make_app()
    with app.app_context():
        admin = User.query.filter_by(email="admin@example.org").one()
        workflow = Workflow(owner_id=admin.id, name="Web download")
        task = WorkflowTask(uid="download", label="download", task_type="web")
        specification = {
            "url": "https://example.test/data.nc",
            "method": "GET",
            "outputs": {"body": "output.nc"},
        }
        task.config = specification
        workflow.tasks.append(task)
        db.session.add(workflow)
        db.session.commit()

        document = workflow.as_json()

    assert document["tasks"]["download"]["specification"] == specification
    assert document["tasks"]["download"]["dagonweb"]["config"] == specification
    runtime_document = dagonstar_document(workflow)
    assert runtime_document["tasks"]["download"]["python"]

    from dagon import Workflow as DagonStarWorkflow
    runtime = DagonStarWorkflow(
        workflow.name,
        config={"batch": {"scratch_dir_base": "/tmp", "remove_dir": False}, "ftp_pub": {}, "dagon_service": {"use": "False"}},
        jsonload=runtime_document,
    )
    assert runtime.tasks[0].specification["url"] == specification["url"]


def test_native_task_serializes_dagonstar_bindings():
    workflow = Workflow(id=7, owner_id=1, name="Native bindings")
    task = WorkflowTask(uid="scale", label="scale", task_type="native")
    task.config = {
        "callable": "analysis_functions:scale_values",
        "inputs": {"input_file": "workflow:///produce/data/values.txt", "factor": 1.5},
        "outputs": {"output_file": "scaled-values.txt"},
        "executor": "local",
        "environment": {"OMP_NUM_THREADS": "1"},
    }
    workflow.tasks.append(task)

    document = workflow.as_json()["tasks"]["scale"]

    assert document["function"] == "analysis_functions:scale_values"
    assert document["inputs"]["input_file"] == "workflow:///produce/data/values.txt"
    assert document["outputs"] == {"output_file": "scaled-values.txt"}
    assert document["environment"] == {"OMP_NUM_THREADS": "1"}


def test_native_source_is_materialized_as_an_importable_module(tmp_path):
    workflow = Workflow(id=11, owner_id=1, name="Embedded native source")
    task = WorkflowTask(uid="double", label="double", task_type="native")
    task.config = {
        "callable": "workflow_functions.math:double",
        "source": "def double(value: int) -> dict:\n    return {'result': value * 2}\n",
    }
    workflow.tasks.append(task)

    root, modules = materialize_native_sources(workflow, tmp_path)

    assert modules == {"workflow_functions.math"}
    assert root is not None
    assert (root / "workflow_functions" / "__init__.py").is_file()
    assert (root / "workflow_functions" / "math.py").read_text(encoding="utf-8").startswith("def double")


def test_python_generator_creates_direct_dagonstar_task_code():
    workflow = Workflow(id=7, owner_id=1, name="Example")
    task = WorkflowTask(uid="a", label="a", task_type="batch")
    task.config = {"command": "echo hello > a.txt"}
    workflow.tasks.append(task)
    source = workflow_python_source(workflow)
    assert "DagonTask(TaskType.BATCH, 'a', 'echo hello > a.txt')" in source
    assert "workflow.add_task(task_a)" in source
    compile(source, "generated_workflow.py", "exec")


def test_graph_validation_rejects_cycles_and_unknown_tasks():
    with pytest.raises(ValueError, match="acyclic"):
        validate_graph_payload({"tasks": {"a": {"name": "a", "type": "batch", "command": "cat workflow:///b/b.txt"}, "b": {"name": "b", "type": "batch", "command": "cat workflow:///a/a.txt"}}})
    with pytest.raises(ValueError, match="missing task"):
        validate_graph_payload({"tasks": {"a": {"name": "a", "type": "batch", "command": "cat workflow:///missing/a.txt"}}})


def test_graph_validation_creates_links_from_workflow_references():
    _, links = validate_graph_payload({"tasks": {"source": {"name": "source", "type": "checkpoint"}, "target": {"name": "target", "type": "batch", "dagonweb": {"config": {"inputs": {"dataset": "workflow:///source/output"}}}}}})
    assert links == [{"source_name": "source", "source_output": "output", "target_name": "target", "target_input": "dataset"}]


def test_graph_validation_creates_links_from_dagonstar_command_references():
    _, links = validate_graph_payload({"tasks": {"a": {"name": "a", "type": "batch", "command": "echo data > a.txt"}, "b": {"name": "b", "type": "batch", "command": "cat workflow:///a/a.txt > b.txt"}}})
    assert links == [{"source_name": "a", "source_output": "a.txt", "target_name": "b", "target_input": "command"}]


def test_dagonstar_diamond_document_ignores_explicit_edges_and_uses_references():
    document = {"name": "Lesson00", "id": 0, "host": "localhost", "tasks": {"a": {"name": "a", "type": "batch", "command": "echo hello > a.txt", "nexts": ["b", "c"]}, "b": {"name": "b", "type": "batch", "command": "cat workflow:///a/a.txt > b.txt", "nexts": ["d"]}, "c": {"name": "c", "type": "batch", "command": "cat workflow:///a/a.txt > c.txt", "nexts": ["d"]}, "d": {"name": "d", "type": "batch", "command": "cat workflow:///b/b.txt; cat workflow:///c/c.txt", "nexts": []}}}
    tasks, links = validate_graph_payload(document)
    assert {task["name"] for task in tasks} == {"a", "b", "c", "d"}
    assert {(link["source_name"], link["target_name"]) for link in links} == {("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")}
    apply_import_layout(tasks, links)
    positions = {task["name"]: (task["x"], task["y"]) for task in tasks}
    assert positions["a"][0] < positions["b"][0] < positions["d"][0]
    assert positions["b"][0] == positions["c"][0]
    assert positions["b"][1] != positions["c"][1]


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
        # Task artifacts must live below the configured scratch root.
        Setting.query.filter_by(key="scratch_dir").one().value = str(tmp_path)
        db.session.commit()
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


def test_run_status_includes_live_run_and_task_logs(tmp_path):
    app = make_app()
    with app.app_context():
        owner = User(email="logs@example.org", name="Logs", active=True)
        owner.set_password("password123")
        db.session.add(owner)
        db.session.flush()
        workflow = Workflow(owner_id=owner.id, name="Logged workflow")
        db.session.add(workflow)
        db.session.flush()
        run = WorkflowRun(workflow_id=workflow.id, user_id=owner.id, status="running", scratch_path=str(tmp_path), log="[time] Workflow started.\n")
        db.session.add(run)
        db.session.flush()
        db.session.add(TaskRun(workflow_run_id=run.id, task_uid="task", status="running", scratch_path=str(tmp_path), log="[time] Task task started.\n"))
        db.session.commit()
        owner_id, workflow_id, run_id = str(owner.id), workflow.id, run.id

    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = owner_id
        session["_fresh"] = True
    detail = client.get(f"/workflows/{workflow_id}/runs/{run_id}")
    assert detail.status_code == 200
    assert b"Run workflow" in detail.data
    assert b"Live execution log" in detail.data
    response = client.get(f"/workflows/{workflow_id}/runs/{run_id}/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["log"] == "[time] Workflow started.\n"
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["name"] == "task"
    assert payload["tasks"][0]["log"] == "[time] Task task started.\n"
