"""DAGonStar 2.9 alignment: task types, explicit edges, runtime, exports."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import create_app, seed_defaults
from app.config import TestConfig
from app.executor.local import build_runtime_workflow, execute_workflow
from app.extensions import db
from app.models import LINK_EXPLICIT, Setting, TaskType, User, Workflow, WorkflowLink, WorkflowRun, WorkflowTask
from app.task_specs import TASK_SPECS, config_from_document, dagon_task_arguments, normalize_command_references, palette, runtime_command
from app.workflows.routes import apply_workflow_document, apply_workflow_template, validate_graph_payload, workflow_python_source

LOCAL_CONFIG = {"batch": {"scratch_dir_base": "/tmp", "remove_dir": False}, "ftp_pub": {}, "dagon_service": {"use": "False"}}


@pytest.fixture()
def app(tmp_path):
    application = create_app(TestConfig)
    application.config["DAGON_INI_PATH"] = str(tmp_path / "dagon.ini")
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


def new_workflow(name: str, template: str | None = None) -> Workflow:
    admin = User.query.filter_by(email="admin@example.org").one()
    workflow = Workflow(owner_id=admin.id, name=name)
    db.session.add(workflow)
    db.session.flush()
    if template:
        apply_workflow_template(workflow, template)
    db.session.commit()
    return workflow


def test_registry_covers_every_dagonstar_task_type():
    from dagon.task import TaskType as DagonTaskType

    native = {key for key, spec in TASK_SPECS.items() if not spec.interactive}
    assert {member.value for member in DagonTaskType} == native
    assert set(TASK_SPECS) == {member.value for member in TaskType}
    # DAGonWeb interactive types always run as a DAGonStar type.
    assert {spec.runtime_type for spec in TASK_SPECS.values()} <= {member.value for member in DagonTaskType}
    grouped = [spec["type"] for group in palette() for spec in group["types"]]
    assert sorted(grouped) == sorted(TASK_SPECS)
    for spec in TASK_SPECS.values():
        assert spec.fields, spec.type
        assert all({"key", "label", "kind"} <= set(field) for field in spec.fields)


@pytest.mark.parametrize("task_type", sorted(TASK_SPECS))
def test_default_configuration_builds_a_dagonstar_task(task_type, tmp_path, monkeypatch):
    from dagon import Workflow as DagonStarWorkflow
    from dagon.task import DagonTask, TaskType as DagonTaskType

    config = {**LOCAL_CONFIG, "agent_provider.fixture": {"implementation": "mock"}, "agent.quality-local": {"provider": "fixture"}}
    runtime = DagonStarWorkflow("Registry", config=config, portable_emulation=True)
    spec = TASK_SPECS[task_type]
    default_config = json.loads(json.dumps(spec.default_config))
    if task_type == "native":
        (tmp_path / "workflow_functions.py").write_text(default_config["source"], encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
    args, kwargs = dagon_task_arguments(task_type, spec.default_name, default_config)
    task = DagonTask(DagonTaskType[spec.runtime_type.upper()], *args, **kwargs)
    runtime.add_task(task)
    runtime.make_dependencies()
    assert task.name == spec.default_name


def test_command_references_are_space_terminated_for_dagonstar():
    assert normalize_command_references("cat workflow:///a/x.txt; echo") == "cat workflow:///a/x.txt ; echo"
    assert normalize_command_references("cat workflow:///a/x.txt\necho") == "cat workflow:///a/x.txt \necho"
    assert normalize_command_references("cat workflow:///a/x.txt > y") == "cat workflow:///a/x.txt > y"
    command = runtime_command("batch", {"command": "wc -l $data", "inputs": {"data": "workflow:///a/x.txt", "label": "two words"}})
    assert command == "data=workflow:///a/x.txt ;\nlabel='two words' ;\nwc -l $data"
    assert runtime_command("checkpoint", {"command": "", "inputs": {"state": "workflow:///a/x.txt"}}) == "workflow:///a/x.txt"


def test_dagonstar_json_loader_accepts_dagonweb_documents(app):
    """DAGonStar's own ``load_json`` understands the exported task map."""
    from dagon import Workflow as DagonStarWorkflow

    with app.app_context():
        workflow = new_workflow("Loader_check", "faas")
        for task_type in ("iot", "web", "llm"):
            spec = TASK_SPECS[task_type]
            task = WorkflowTask(workflow_id=workflow.id, uid=spec.default_name, label=spec.default_name, task_type=task_type)
            task.config = spec.default_config
            workflow.tasks.append(task)
        db.session.commit()
        document = workflow.as_json()
    loaded = DagonStarWorkflow("Loader_check", config=LOCAL_CONFIG, jsonload=document)
    assert {task.name for task in loaded.tasks} == set(document["tasks"])
    faas = next(task for task in loaded.tasks if task.name == "invoke")
    assert faas.provider == "mock"
    assert document["tasks"]["iot_task"]["iot"]["operation"] == "observe"


def test_plain_dagonstar_documents_import_type_specific_fields():
    document = {
        "name": "Plain",
        "tasks": {
            "prepare": {"name": "prepare", "type": "batch", "command": "echo 4 > n.txt", "explicit_prevs": []},
            "invoke": {"name": "invoke", "type": "faas", "provider": "mock", "function": "echo", "inputs": {"n": "workflow:///prepare/n.txt"}},
            "observe": {"name": "observe", "type": "iot", "command": None, "iot": {"schemaVersion": 1, "operation": "observe", "provider": "mock", "providerOptions": {"a": 1}}},
            "report": {"name": "report", "type": "batch", "command": "date > r.txt", "explicit_prevs": ["observe"]},
        },
    }
    tasks, links = validate_graph_payload(document)
    by_name = {task["name"]: task for task in tasks}
    assert by_name["invoke"]["config"]["function"] == "echo"
    assert by_name["observe"]["config"]["provider_options"] == {"a": 1}
    assert {(link["kind"], link["source_name"], link["target_name"]) for link in links} == {("data", "prepare", "invoke"), (LINK_EXPLICIT, "observe", "report")}
    assert config_from_document({"type": "checkpoint", "command": "workflow:///a/x.txt"}) == {"inputs": {"ref_1": "workflow:///a/x.txt"}, "command": ""}


def test_explicit_dependencies_round_trip_and_reach_dagonstar(app):
    with app.app_context():
        workflow = new_workflow("Explicit_edges")
        document = {
            "name": "Explicit_edges",
            "pre_stage": True,
            "tasks": {
                "first": {"name": "first", "type": "batch", "dagonweb": {"x": 10, "y": 10, "config": {"command": "date > a.txt"}}},
                "second": {"name": "second", "type": "batch", "explicit_prevs": ["first"], "dagonweb": {"x": 300, "y": 10, "config": {"command": "date > b.txt"}}},
            },
        }
        apply_workflow_document(workflow, document)
        db.session.commit()
        assert [(link.kind, link.source_uid, link.target_uid) for link in workflow.links] == [(LINK_EXPLICIT, "first", "second")]
        exported = workflow.as_json()
        assert exported["tasks"]["second"]["explicit_prevs"] == ["first"]
        assert exported["tasks"]["second"]["prevs"] == ["first"]
        assert exported["pre_stage"] is True
        runtime = build_runtime_workflow(workflow, LOCAL_CONFIG)
        second = next(task for task in runtime.tasks if task.name == "second")
        assert [task.name for task in second.prevs] == ["first"]
        assert runtime.as_json()["tasks"]["second"]["explicit_prevs"] == ["first"]
        assert runtime.pre_stage is True

    with pytest.raises(ValueError, match="acyclic"):
        validate_graph_payload({"tasks": {"a": {"name": "a", "type": "batch", "explicit_prevs": ["b"]}, "b": {"name": "b", "type": "batch", "command": "cat workflow:///a/x"}}})
    with pytest.raises(ValueError, match="unknown task"):
        validate_graph_payload({"tasks": {"a": {"name": "a", "type": "batch", "explicit_prevs": ["ghost"]}}})


def provide_interactive_inputs(workflow: Workflow, run: WorkflowRun) -> None:
    """Pre-fill what Upload/Form tasks wait for, as the run page would."""
    from app.interactive import form_document, form_env, form_fields
    from app.executor.local import run_data_path, scratch_root

    run_dir = scratch_root() / f"workflow-{workflow.id}" / f"run-{run.id}"
    for task in workflow.tasks:
        target = run_data_path(run_dir, task.name)
        if task.normalized_task_type == "upload":
            (target / "files").mkdir(parents=True, exist_ok=True)
            (target / "files" / task.config.get("save_as", "data.csv")).write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
            (target / ".ready").touch()
        elif task.normalized_task_type == "form":
            target.mkdir(parents=True, exist_ok=True)
            values = {field["name"]: field["default"] for field in form_fields(task.config)}
            (target / "form.json").write_text(form_document(values), encoding="utf-8")
            (target / "form.env").write_text(form_env(values), encoding="utf-8")
            (target / ".ready").touch()


@pytest.mark.parametrize("template", sorted(TASK_SPECS))
def test_every_template_runs_end_to_end(app, template):
    with app.app_context():
        workflow = new_workflow(f"E2E_{template}", template)
        run = WorkflowRun(workflow_id=workflow.id, user_id=workflow.owner_id, status="pending", scratch_path="", log="")
        db.session.add(run)
        db.session.commit()
        provide_interactive_inputs(workflow, run)
        run = execute_workflow(workflow, workflow.owner_id, run)
        assert run.status == "success", run.log
        scratch = Path(run.scratch_path)
        assert run.task_runs and all(task_run.status == "success" for task_run in run.task_runs)
        # Every task directory lives inside the run directory.
        assert all(Path(task_run.scratch_path).resolve().parent == scratch.resolve() for task_run in run.task_runs)
        assert "completed." in run.log
        if template == "batch":
            merge = next(task_run for task_run in run.task_runs if task_run.task_uid == "merge")
            assert (Path(merge.scratch_path) / "result.txt").read_text(encoding="utf-8") == "HELLO\nolleh\n"
        if template == "form":
            use = next(task_run for task_run in run.task_runs if task_run.task_uid == "use_values")
            assert (Path(use.scratch_path) / "summary.txt").read_text(encoding="utf-8") == "Threshold 0.5 for experiment\n"
        if template in ("table", "chart", "upload", "download"):
            produced = {path.name for task_run in run.task_runs for path in Path(task_run.scratch_path).glob("outputs/*")}
            assert {"table.json", "chart.json", "chart.svg", "results.zip"} & produced


def test_failed_task_error_is_logged_and_run_resumes_from_checkpoint(app):
    with app.app_context():
        workflow = new_workflow("Resumable")
        apply_workflow_document(workflow, {
            "name": "Resumable",
            "tasks": {
                "produce": {"name": "produce", "type": "batch", "dagonweb": {"config": {"command": "echo data > data.txt"}}},
                "consume": {"name": "consume", "type": "batch", "dagonweb": {"config": {"command": "test -f ../allow || { echo 'not yet' >&2; exit 3; }\ncat workflow:///produce/data.txt > copy.txt"}}},
                "after": {"name": "after", "type": "batch", "explicit_prevs": ["consume"], "dagonweb": {"config": {"command": "echo done > done.txt"}}},
            },
        })
        db.session.commit()
        run = execute_workflow(workflow, workflow.owner_id)
        assert run.status == "failed"
        assert "Task consume error:" in run.log and "not yet" in run.log
        assert "Task after error: skipped because an upstream task failed" in run.log
        produce_dir = next(task_run.scratch_path for task_run in run.task_runs if task_run.task_uid == "produce")

        (Path(run.scratch_path) / "allow").write_text("", encoding="utf-8")
        run = execute_workflow(workflow, workflow.owner_id, run, resume=True)
        assert run.status == "success", run.log
        assert "Workflow resumed from checkpoint." in run.log
        statuses = {task_run.task_uid: task_run for task_run in run.task_runs}
        assert statuses["produce"].scratch_path == produce_dir
        assert (Path(statuses["consume"].scratch_path) / "copy.txt").read_text(encoding="utf-8") == "data\n"


def test_generated_python_program_runs_with_dagonstar(app, tmp_path):
    with app.app_context():
        workflow = new_workflow("Generated", "batch")
        source = workflow_python_source(workflow)
    assert "TaskType.BATCH,\n        'transform_a',\n        (\n            'data=workflow:///extract/data.txt ;\\n'\n            'tr a-z A-Z < $data > a.txt'\n        ),\n    )" in source
    assert "# Reads: workflow:///extract/data.txt" in source
    program = tmp_path / "Generated.py"
    program.write_text(source, encoding="utf-8")
    completed = subprocess.run([sys.executable, str(program)], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "Workflow completed." in completed.stdout
    assert (tmp_path / "scratch" / "checkpoint.json").is_file()


@pytest.mark.parametrize("template", ["native", "agentic", "faas"])
def test_generated_python_compiles_for_structured_tasks(app, template):
    with app.app_context():
        workflow = new_workflow(f"Generated_{template}", template)
        source = workflow_python_source(workflow)
    compile(source, f"{template}.py", "exec")
    if template == "native":
        assert "NATIVE_SOURCES" in source
    if template == "agentic":
        assert "'agent.quality-local'" in source


def test_cwl_export_endpoint(app):
    with app.app_context():
        workflow_id = new_workflow("Cwl_export", "batch").id
    response = login(app).get(f"/workflows/{workflow_id}/cwl")
    assert response.status_code == 200
    assert b"cwlVersion" in response.data
    assert "attachment" in response.headers["Content-Disposition"]


def test_setup_saves_runtime_options_and_keeps_agent_profiles(app):
    with app.app_context():
        workflow_id = new_workflow("Runtime_options", "agentic").id
    client = login(app)
    response = client.post(f"/workflows/{workflow_id}/setup", data={"name": "Runtime_options", "batch_threads": "2", "max_threads": "4", "portable_emulation": "y", "pre_stage": "y"})
    assert response.status_code == 302
    with app.app_context():
        workflow = db.session.get(Workflow, workflow_id)
        assert workflow.runtime_options == {"portable_emulation": True, "pre_stage": True, "max_threads": 4}
        assert workflow.dagon_ini["agent.quality-local"]["provider"] == "fixture"
        assert workflow.dagon_ini["batch"] == {"threads": "2"}


def test_workflow_setup_cannot_move_scratch_outside_root(app, tmp_path):
    from app.executor.local import workflow_config

    with app.app_context():
        workflow = new_workflow("Scratch_guard")
        workflow.dagon_ini = {"batch": {"scratch_dir_base": "/etc", "remove_dir": "True"}}
        config = workflow_config(workflow, tmp_path / "run")
    assert config["batch"]["scratch_dir_base"] == str(tmp_path / "run")
    assert config["batch"]["remove_dir"] is False


def test_active_runs_block_workflow_deletion_and_resume_requires_failure(app):
    with app.app_context():
        workflow = new_workflow("Busy", "batch")
        run = WorkflowRun(workflow_id=workflow.id, user_id=workflow.owner_id, status="running", scratch_path="", log="")
        db.session.add(run)
        db.session.commit()
        workflow_id, run_id = workflow.id, run.id
    client = login(app)
    client.post(f"/workflows/{workflow_id}/delete")
    with app.app_context():
        assert db.session.get(Workflow, workflow_id) is not None
    response = client.post(f"/workflows/runs/{run_id}/resume")
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(WorkflowRun, run_id).status == "running"


def test_editor_receives_the_task_registry(app):
    with app.app_context():
        workflow_id = new_workflow("Registry_page").id
    response = login(app).get(f"/workflows/{workflow_id}/editor")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="taskSpecs"' in page
    for spec in TASK_SPECS.values():
        assert f'data-type="{spec.type}"' in page


def test_explicit_link_kind_defaults_to_data():
    link = WorkflowLink(source_uid="a", source_output="x", target_uid="b", target_input="input", workflow_uri="workflow:///a/x")
    assert link.to_dict()["kind"] in {None, "data"}


EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_examples_match_the_builtin_templates(app):
    from app.workflows.routes import template_document, workflow_template_options

    with app.app_context():
        admin = User.query.filter_by(email="admin@example.org").one()
        for key, _label in workflow_template_options():
            path = EXAMPLES / f"{key}.json"
            assert path.exists(), f"missing {path}; run `flask --app wsgi export-examples`"
            expected = json.loads(json.dumps(template_document(key, admin.id), sort_keys=True))
            assert json.loads(path.read_text(encoding="utf-8")) == expected, f"{path} is stale; run `flask --app wsgi export-examples`"


@pytest.mark.parametrize("example", sorted(path.name for path in EXAMPLES.glob("*.json")))
def test_examples_import_and_build(app, example):
    document = json.loads((EXAMPLES / example).read_text(encoding="utf-8"))
    with app.app_context():
        workflow = new_workflow(document["name"])
        apply_workflow_document(workflow, document)
        db.session.commit()
        assert sorted(task.name for task in workflow.tasks) == sorted(document["tasks"])
        expected_links = sum(len(task["prevs"]) for task in document["tasks"].values())
        assert len(workflow.links) == expected_links
