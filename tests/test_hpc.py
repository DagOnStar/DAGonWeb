"""HPC cluster execution as the users' own Unix accounts (app/hpc.py, hpc/dagonweb_runner.py).

sshd is simulated: ``FakeSSH`` only accepts a key whose public half was
"added to authorized_keys" and then starts the real runner as a forced
command, with ``SSH_ORIGINAL_COMMAND`` set, exactly as sshd would.
"""
from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from app import create_app, hpc, privacy, seed_defaults, settings
from app.config import TestConfig
from app.executor.dispatch import run_job
from app.executor.remote import build_run_spec
from app.extensions import db
from app.models import AuditLog, Role, Setting, TaskRun, User, Workflow, WorkflowRun
from app.workflows.routes import apply_workflow_document, apply_workflow_template, remote_relative_path

RUNNER = Path(__file__).resolve().parent.parent / "hpc" / "dagonweb_runner.py"
UNIX_USER = pwd.getpwuid(os.getuid()).pw_name
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


class FakeSSH:
    """sshd + authorized_keys stand-in that runs the real forced command."""

    def __init__(self, runner_config: Path) -> None:
        self.authorized: set[str] = set()
        self.config = runner_config
        self.commands: list[str] = []

    def factory(self, user: User, private_key: str):
        from cryptography.hazmat.primitives import serialization

        public = serialization.load_ssh_private_key(private_key.encode(), None).public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        fake = self

        class Transport:
            def exec(self, command: str, stdin: bytes | None = None, max_output: int = 0):
                if public not in fake.authorized:
                    raise hpc.HpcError(f"The cluster refused the key for {user.unix_username}.")
                fake.commands.append(command)
                env = {**os.environ, "SSH_ORIGINAL_COMMAND": command, "DAGONWEB_RUNNER_CONFIG": str(fake.config)}
                done = subprocess.run([sys.executable, str(RUNNER)], input=stdin or b"", capture_output=True, env=env, timeout=120)
                return done.returncode, done.stdout

            def close(self) -> None:
                pass

        return Transport()

    def add(self, user: User) -> None:
        self.authorized.add(" ".join(user.unix_public_key.split()[:2]))


@pytest.fixture()
def cluster(tmp_path):
    config = tmp_path / "runner.json"
    config.write_text(json.dumps({"base": str(tmp_path / "cluster"), "min_uid": 0}), encoding="utf-8")
    return FakeSSH(config)


@pytest.fixture()
def app(tmp_path, cluster):
    application = create_app(TestConfig)
    application.config["DAGON_INI_PATH"] = str(tmp_path / "dagon.ini")
    application.config["HPC_TRANSPORT_FACTORY"] = cluster.factory
    application.instance_path = str(tmp_path / "instance")
    with application.app_context():
        db.create_all()
        seed_defaults(application)
        Setting.query.filter_by(key="scratch_dir").one().value = str(tmp_path / "scratch")
        for key, value in {"hpc_enabled": True, "hpc_ssh_host_key": HOST_KEY, "hpc_source_cidr": "172.16.0.0/12", "hpc_poll_seconds": 0.2, "hpc_min_uid": 0}.items():
            settings.set_value(key, value)
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


def make_user(email: str) -> User:
    user = User(email=email, name=email, active=True, roles=Role.query.filter(Role.name == "user").all())
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()
    return user


def link(cluster: FakeSSH, email: str = "admin@example.org", name: str = UNIX_USER) -> User:
    user = User.query.filter_by(email=email).one()
    hpc.start_link(user, name)
    cluster.add(user)
    hpc.verify_link(user)
    db.session.commit()
    return user


def new_workflow(name: str, owner: str = "admin@example.org", template: str | None = None, document: dict | None = None) -> Workflow:
    workflow = Workflow(owner_id=User.query.filter_by(email=owner).one().id, name=name)
    db.session.add(workflow)
    db.session.flush()
    if template:
        apply_workflow_template(workflow, template)
    if document:
        apply_workflow_document(workflow, document)
    db.session.commit()
    return workflow


def start(workflow: Workflow, user: User) -> WorkflowRun:
    run = WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="pending", scratch_path="", log="", unix_username=user.unix_username)
    db.session.add(run)
    db.session.commit()
    run_job(workflow.id, run.id)
    db.session.expire_all()
    return db.session.get(WorkflowRun, run.id)


# ---------------------------------------------------------------- keys and linking


def test_keys_are_encrypted_and_the_authorized_keys_line_is_restricted(app):
    with app.app_context():
        user = User.query.filter_by(email="admin@example.org").one()
        hpc.start_link(user, "alice")
        assert user.unix_private_key and "PRIVATE KEY" not in user.unix_private_key
        assert "OPENSSH PRIVATE KEY" in hpc.decrypt_private_key(user.unix_private_key)
        line = hpc.authorized_keys_line(user)
        assert line.startswith('restrict,from="172.16.0.0/12",command="/opt/dagonweb/bin/dagonweb-runner" ssh-ed25519 ')
        assert line.endswith(f"dagonweb-account-{user.id}")
        app.config["HPC_KEY_SECRET"] = "another secret"
        with pytest.raises(hpc.HpcError, match="cannot be decrypted"):
            hpc.decrypt_private_key(user.unix_private_key)


@pytest.mark.parametrize("name", ["root", "slurm", "Alice", "a b", "../etc", "x" * 40, ""])
def test_system_and_malformed_user_names_are_refused(name):
    with pytest.raises(ValueError):
        hpc.validate_username(name)


def test_linking_needs_the_key_in_authorized_keys(app, cluster):
    client = login(app)
    response = client.post("/account/cluster", data={"cluster-unix_username": UNIX_USER})
    assert response.status_code == 302
    page = client.get("/account/").get_data(as_text=True)
    assert "authorized_keys" in page and "restrict,from=" in page and "Verify cluster account" in page
    # Not yet added: the cluster refuses the key.
    client.post("/account/cluster/verify", data={})
    with app.app_context():
        user = User.query.filter_by(email="admin@example.org").one()
        assert not user.unix_linked
        assert AuditLog.query.filter_by(action="account.cluster_verify_failed").count() == 1
        cluster.add(user)
    client.post("/account/cluster/verify", data={})
    with app.app_context():
        user = User.query.filter_by(email="admin@example.org").one()
        assert user.unix_linked and user.unix_username == UNIX_USER
        assert AuditLog.query.filter_by(action="account.cluster_linked").count() == 1
    assert "Unlink cluster account" in client.get("/account/").get_data(as_text=True)
    client.post("/account/cluster/unlink", data={})
    with app.app_context():
        user = User.query.filter_by(email="admin@example.org").one()
        assert user.unix_username is None and user.unix_private_key is None


def test_a_key_that_lands_in_another_account_is_refused(app, cluster):
    with app.app_context():
        user = User.query.filter_by(email="admin@example.org").one()
        hpc.start_link(user, "someoneelse")  # the runner reports the real user
        cluster.add(user)
        with pytest.raises(hpc.HpcError, match="not 'someoneelse'"):
            hpc.verify_link(user)
        assert not user.unix_linked


def test_a_linked_cluster_account_cannot_be_claimed_twice(app, cluster):
    with app.app_context():
        link(cluster)
        other = make_user("other@example.org")
        with pytest.raises(ValueError, match="already linked"):
            hpc.start_link(other, UNIX_USER)


def test_system_uids_are_refused_by_dagonweb_too(app, cluster):
    with app.app_context():
        settings.set_value("hpc_min_uid", os.getuid() + 1)
        db.session.commit()
        user = User.query.filter_by(email="admin@example.org").one()
        hpc.start_link(user, UNIX_USER)
        cluster.add(user)
        with pytest.raises(hpc.HpcError, match="System accounts"):
            hpc.verify_link(user)


# ---------------------------------------------------------------- runs


def test_runs_need_a_linked_account_and_never_run_locally(app, cluster):
    client = login(app)
    with app.app_context():
        workflow = new_workflow("NeedsLink", template="batch")
        workflow_id = workflow.id
    response = client.post(f"/workflows/{workflow_id}/run", data={})
    assert response.status_code == 302 and "/account/" in response.headers["Location"]
    with app.app_context():
        assert WorkflowRun.query.count() == 0
        # A run record without a cluster account is refused by the dispatcher.
        workflow = db.session.get(Workflow, workflow_id)
        run = WorkflowRun(workflow_id=workflow.id, user_id=workflow.owner_id, status="pending", scratch_path="", log="")
        db.session.add(run)
        db.session.commit()
        run_job(workflow.id, run.id)
        db.session.expire_all()
        run = db.session.get(WorkflowRun, run.id)
        assert run.status == "failed" and "cluster execution is enabled" in run.log
        assert not run.task_runs


@pytest.mark.parametrize("template", ["batch", "native", "chart", "slurm"])
def test_templates_run_on_the_cluster_as_the_user(app, cluster, template, tmp_path):
    with app.app_context():
        user = link(cluster)
        workflow = new_workflow(f"Cluster_{template}", template=template)
        run = start(workflow, user)
        assert run.status == "success", run.log
        assert run.unix_username == UNIX_USER
        assert run.scratch_path == str(tmp_path / "cluster" / f"run_{run.id}")
        assert "Submitting to host.docker.internal as" in run.log and "completed with status: success" in run.log
        assert run.task_runs and all(task_run.status == "success" for task_run in run.task_runs)
        assert all(Path(task_run.scratch_path).parent == Path(run.scratch_path) for task_run in run.task_runs)
        # Nothing was executed inside DAGonWeb's own scratch directory.
        assert not (tmp_path / "scratch" / f"workflow-{workflow.id}").exists()
        assert all(command.split()[0] in {"ping", "submit", "status"} for command in cluster.commands)


def test_cluster_run_files_are_browsed_and_downloaded_through_the_runner(app, cluster, tmp_path):
    with app.app_context():
        user = link(cluster)
        workflow = new_workflow("Browse", template="batch")
        run = start(workflow, user)
        merge = next(task_run for task_run in run.task_runs if task_run.task_uid == "merge")
        ids = (workflow.id, run.id, merge.id)
    client = login(app)
    listing = client.get(f"/workflows/task-runs/{ids[2]}/files").get_json()
    assert "result.txt" in {item["name"] for item in listing["files"]}
    preview = client.get(f"/workflows/task-runs/{ids[2]}/file?path=result.txt").get_json()
    assert preview == {"path": "result.txt", "kind": "text", "content": "HELLO\nolleh\n"}
    download = client.get(f"/workflows/task-runs/{ids[2]}/download?path=result.txt")
    assert download.status_code == 200 and download.data == b"HELLO\nolleh\n"
    assert client.get(f"/workflows/task-runs/{ids[2]}/file?path=../../../../etc/passwd").status_code in (400, 403)
    assert client.get(f"/workflows/task-runs/{ids[2]}/download?path=../../outside").status_code == 403
    assert client.get(f"/workflows/task-runs/{ids[2]}/archive").status_code == 501
    # Other users cannot see the files.
    with app.app_context():
        make_user("other@example.org")
    assert login(app, "other@example.org").get(f"/workflows/task-runs/{ids[2]}/files").status_code == 403
    # Deleting the run removes its folder on the cluster.
    assert client.post(f"/workflows/runs/{ids[1]}/delete").status_code == 302
    assert not (tmp_path / "cluster" / f"run_{ids[1]}").exists()


def test_failed_cluster_run_resumes_from_its_checkpoint(app, cluster):
    with app.app_context():
        user = link(cluster)
        workflow = new_workflow("ResumeCluster", document={
            "name": "ResumeCluster",
            "tasks": {
                "produce": {"name": "produce", "type": "batch", "dagonweb": {"config": {"command": "echo data > data.txt"}}},
                "consume": {"name": "consume", "type": "batch", "dagonweb": {"config": {"command": "test -f ../allow || { echo 'not yet' >&2; exit 3; }\ncat workflow:///produce/data.txt > copy.txt"}}},
            },
        })
        run = start(workflow, user)
        assert run.status == "failed"
        assert "Task consume error:" in run.log and "not yet" in run.log
        produce_dir = next(task_run.scratch_path for task_run in run.task_runs if task_run.task_uid == "produce")
        run_id, workflow_id, run_dir = run.id, workflow.id, run.scratch_path
    (Path(run_dir) / "allow").write_text("", encoding="utf-8")
    # Another administrator may not resume it: it executes as the submitter.
    with app.app_context():
        other = make_user("admin2@example.org")
        other.roles = Role.query.filter(Role.name == "admin").all()
        db.session.commit()
    response = login(app, "admin2@example.org").post(f"/workflows/runs/{run_id}/resume")
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(WorkflowRun, run_id).status == "failed"
        run_job(workflow_id, run_id, resume=True)
        db.session.expire_all()
        run = db.session.get(WorkflowRun, run_id)
        assert run.status == "success", run.log
        assert "resumed from checkpoint on the cluster" in run.log
        statuses = {task_run.task_uid: task_run for task_run in run.task_runs}
        assert statuses["produce"].scratch_path == produce_dir
        assert (Path(statuses["consume"].scratch_path) / "copy.txt").read_text(encoding="utf-8") == "data\n"


def test_an_admin_runs_someone_elses_workflow_as_their_own_account(app, cluster):
    with app.app_context():
        other = make_user("owner@example.org")
        workflow = new_workflow("Shared", owner="owner@example.org", template="batch")
        workflow.is_public = True
        db.session.commit()
        link(cluster)  # the administrator's own cluster account
        workflow_id, other_id = workflow.id, other.id
    client = login(app)
    response = client.post(f"/workflows/{workflow_id}/run", data={})
    assert response.status_code == 302
    with app.app_context():
        run = WorkflowRun.query.one()
        admin = User.query.filter_by(email="admin@example.org").one()
        assert run.user_id == admin.id != other_id
        assert run.unix_username == admin.unix_username


def test_upload_and_form_tasks_are_refused_on_the_cluster(app, cluster):
    with app.app_context():
        link(cluster)
        workflow = new_workflow("Interactive", template="upload")
        workflow_id = workflow.id
        with pytest.raises(ValueError, match="Upload and Form"):
            build_run_spec(workflow, "/cluster/run_1", "python3")
    response = login(app).post(f"/workflows/{workflow_id}/run", data={})
    assert response.status_code == 302
    with app.app_context():
        assert WorkflowRun.query.count() == 0


def test_task_paths_cannot_escape_the_task_directory(app):
    with app.app_context():
        user = User.query.filter_by(email="admin@example.org").one()
        workflow = new_workflow("Paths")
        run = WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="success", scratch_path="/c/run_1", log="", unix_username="x")
        db.session.add(run)
        db.session.flush()
        task_run = TaskRun(workflow_run_id=run.id, task_uid="t", status="success", scratch_path="/c/run_1/123-t", log="")
        db.session.add(task_run)
        db.session.flush()
        assert remote_relative_path(task_run, "out/a.txt") == "123-t/out/a.txt"
        assert remote_relative_path(task_run, "") == "123-t"
        for bad in ("../other", "../../etc/passwd", "a/../../x"):
            with pytest.raises(ValueError):
                remote_relative_path(task_run, bad)
        task_run.scratch_path = "/elsewhere"
        with pytest.raises(ValueError):
            remote_relative_path(task_run, "")


# ---------------------------------------------------------------- runner (forced command)


def runner(command: str, config: Path, stdin: bytes = b"") -> tuple[int, dict]:
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": command, "DAGONWEB_RUNNER_CONFIG": str(config)}
    done = subprocess.run([sys.executable, str(RUNNER)], input=stdin, capture_output=True, env=env, timeout=60)
    return done.returncode, json.loads(done.stdout.decode().strip().splitlines()[-1])


def test_runner_accepts_only_its_own_verbs(cluster):
    for command in ("bash -i", "rm -rf ~", "_execute 1", "ping; id", "submit ../1", "status 1 0 extra", "ls abc", "'unterminated"):
        code, result = runner(command, cluster.config)
        assert code != 0 and result["ok"] is False, command
    code, result = runner("ping", cluster.config)
    assert code == 0 and result["user"] == UNIX_USER


def test_runner_refuses_system_uids_and_direct_logins(cluster, tmp_path):
    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps({"base": str(tmp_path / "cluster"), "min_uid": os.getuid() + 1}), encoding="utf-8")
    code, result = runner("ping", strict)
    assert code == 77 and "refuses" in result["error"]
    env = {key: value for key, value in os.environ.items() if key != "SSH_ORIGINAL_COMMAND"}
    env["DAGONWEB_RUNNER_CONFIG"] = str(cluster.config)
    done = subprocess.run([sys.executable, str(RUNNER)], capture_output=True, env=env, timeout=60)
    assert done.returncode == 64 and b"forced command" in done.stdout


def test_runner_keeps_file_access_inside_the_run_directory(cluster, tmp_path):
    (tmp_path / "cluster" / "run_5").mkdir(parents=True)
    (tmp_path / "cluster" / "run_5" / "ok.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "cluster" / "run_5" / "link").symlink_to(tmp_path / "secret.txt")
    assert runner("ls 5", cluster.config)[1]["files"] == [{"name": "ok.txt", "is_dir": False, "size": 2, "modified_at": pytest.approx((tmp_path / "cluster" / "run_5" / "ok.txt").stat().st_mtime)}]
    for command in ("get 5 link", "get 5 ../../secret.txt", "ls 5 ..", "get 5 /etc/passwd"):
        code, result = runner(command, cluster.config)
        assert code != 0 and result["ok"] is False, command


def test_runner_rejects_malformed_specs(cluster):
    for spec in (b"not json", b"[]", json.dumps({"name": "x", "tasks": [{"name": "../a", "type": "batch", "args": []}]}).encode(),
                 json.dumps({"name": "x", "tasks": [], "explicit": [["a", "b"]]}).encode()):
        code, result = runner("submit 9", cluster.config, spec)
        assert code != 0 and result["ok"] is False


# ---------------------------------------------------------------- settings and GDPR


def test_hpc_settings_are_validated(app):
    client = login(app)
    with app.app_context():
        settings.set_value("hpc_ssh_host_key", "")
        db.session.commit()
    response = client.post("/admin/settings/hpc", data={"hpc_enabled": "on", "hpc_ssh_host": "login01", "hpc_ssh_port": "22", "hpc_ssh_host_key": "", "hpc_runner_command": "/opt/dagonweb/bin/dagonweb-runner", "hpc_source_cidr": "", "hpc_min_uid": "1000", "hpc_poll_seconds": "5"})
    assert response.status_code == 200 and "host key is required" in response.get_data(as_text=True)
    for key, value in {"hpc_ssh_host_key": "not a key", "hpc_source_cidr": "nowhere", "hpc_runner_command": "rm -rf /", "hpc_ssh_host": "a b"}.items():
        with pytest.raises(ValueError):
            settings.validate(settings.DEFINITIONS[key], value)
    assert settings.validate(settings.DEFINITIONS["hpc_ssh_host_key"], "login01 " + HOST_KEY) == "login01 " + HOST_KEY


def test_cluster_account_is_exported_without_the_private_key(app, cluster):
    with app.app_context():
        user = link(cluster)
        data = privacy.export_user_data(user)
        assert data["account"]["cluster_account"]["unix_username"] == UNIX_USER
        assert "PRIVATE" not in json.dumps(data) and user.unix_private_key not in json.dumps(data)
