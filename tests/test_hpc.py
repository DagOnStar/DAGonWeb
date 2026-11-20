"""HPC cluster execution as the users' own Unix accounts (app/hpc.py, hpc/dagonweb_runner.py).

sshd is simulated: ``FakeSSH`` only accepts a key whose public half was
"added to authorized_keys" and then starts the real runner as a forced
command, with ``SSH_ORIGINAL_COMMAND`` set, exactly as sshd would.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import zipfile
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import create_app, hpc, privacy, seed_defaults, settings, sshsig
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
    # A file database: the run follower and the web requests use separate
    # threads, and the shared in-memory connection is not thread-safe.
    config = type("HpcTestConfig", (TestConfig,), {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'dagonweb.db'}"})
    application = create_app(config)
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
        assert all(command.split()[0] in {"ping", "submit", "status", "ls", "get"} for command in cluster.commands)


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
    archive = client.get(f"/workflows/task-runs/{ids[2]}/archive")
    assert archive.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(archive.data)).read("result.txt") == b"HELLO\nolleh\n"
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


def start_in_background(app, workflow_id: int, run_id: int) -> threading.Thread:
    def target() -> None:
        with app.app_context():
            run_job(workflow_id, run_id)

    thread = threading.Thread(target=target, daemon=True)  # a failed test must not keep pytest alive
    thread.start()
    return thread


def wait_for(app, run_id: int, predicate, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with app.app_context():
            run = db.session.get(WorkflowRun, run_id)
            if predicate(run):
                return
        time.sleep(0.2)
    raise AssertionError("timed out waiting for the run")


@pytest.mark.parametrize("template", ["upload", "form"])
def test_upload_and_form_inputs_reach_the_cluster_run(app, cluster, template, tmp_path):
    with app.app_context():
        user = link(cluster)
        workflow = new_workflow(f"Interactive_{template}", template=template)
        run = WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="pending", scratch_path="", log="", unix_username=user.unix_username)
        db.session.add(run)
        db.session.commit()
        workflow_id, run_id = workflow.id, run.id
        waiting = next(task.name for task in workflow.tasks if task.normalized_task_type == template)
    thread = start_in_background(app, workflow_id, run_id)
    client = login(app)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        interactions = client.get(f"/workflows/{workflow_id}/runs/{run_id}/status").get_json()["interactions"]
        if interactions:
            break
        time.sleep(0.2)
    assert interactions and interactions[0]["task"] == waiting
    # Another user (even an administrator) cannot write into this user's run.
    with app.app_context():
        other = make_user("other-admin@example.org")
        other.roles = Role.query.filter(Role.name == "admin").all()
        db.session.commit()
    assert login(app, "other-admin@example.org").post(f"/workflows/runs/{run_id}/interact/{waiting}", json={}).status_code == 403
    if template == "upload":
        response = client.post(f"/workflows/runs/{run_id}/interact/{waiting}", data={"files": (io.BytesIO(b"a,b\n1,2\n3,4\n"), "data.csv")}, content_type="multipart/form-data")
    else:
        response = client.post(f"/workflows/runs/{run_id}/interact/{waiting}", json={"threshold": "0.75", "label": "cluster"})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert client.post(f"/workflows/runs/{run_id}/interact/{waiting}", json={}).status_code == 409
    thread.join(timeout=120)
    with app.app_context():
        run = db.session.get(WorkflowRun, run_id)
        assert run.status == "success", run.log
        assert f"Input for {waiting} provided" in run.log
        assert (tmp_path / "cluster" / f"run_{run_id}" / "_dagonweb" / "data" / waiting / ".ready").is_file()


def test_table_chart_and_download_results_are_shown_for_cluster_runs(app, cluster):
    with app.app_context():
        user = link(cluster)
        results = {}
        for template in ("table", "chart", "download"):
            workflow = new_workflow(f"Results_{template}", template=template)
            run = start(workflow, user)
            assert run.status == "success", run.log
            results[template] = (workflow.id, run.id)
    client = login(app)
    kinds = set()
    for template, (workflow_id, run_id) in results.items():
        commands_before = len(cluster.commands)
        status = client.get(f"/workflows/{workflow_id}/runs/{run_id}/status").get_json()
        assert len(cluster.commands) == commands_before  # status polls never call the cluster
        for item in status["results"]:
            kinds.add(item["kind"])
            if item["kind"] in ("table", "chart"):
                assert item["data"]
            if item["kind"] == "download":
                assert client.get(item["url"]).status_code == 200
    assert {"table", "chart", "download"} <= kinds


def test_fair_exports_are_recorded_on_the_cluster(app, cluster):
    with app.app_context():
        user = link(cluster)
        workflow = new_workflow("FairCluster", template="batch")
        workflow.fair_profile = {"enabled": True, "title": "FAIR on the cluster", "creators": [{"name": "Ada"}], "license": "CC-BY-4.0", "access_policy": "public"}
        db.session.commit()
        run = start(workflow, user)
        assert run.status == "success", run.log
        fair = run.remote_meta.get("fair")
        assert fair and "run.json" in {item["name"] for item in fair["files"]}
        workflow_id, run_id = workflow.id, run.id
    response = login(app).get(f"/workflows/{workflow_id}/runs/{run_id}/fair/run.json")
    assert response.status_code == 200 and json.loads(response.data)


def test_an_orphaned_cluster_run_is_picked_up_again(app, cluster):
    """A web restart loses the following thread; the next status poll reconnects."""
    with app.app_context():
        user = link(cluster)
        workflow = new_workflow("Orphan", document={"name": "Orphan", "tasks": {
            "slow": {"name": "slow", "type": "batch", "dagonweb": {"config": {"command": "sleep 2; echo done > done.txt"}}}}})
        run = WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="pending", scratch_path="", log="", unix_username=user.unix_username)
        db.session.add(run)
        db.session.commit()
        workflow_id, run_id = workflow.id, run.id
        # Submit, then "crash": nobody follows the run any more.
        from app.executor import remote

        original = remote.follow
        remote.follow = lambda client, run_id: None
        try:
            run_job(workflow_id, run_id)
        finally:
            remote.follow = original
        run = db.session.get(WorkflowRun, run_id)
        assert run.status == "running"
        run.monitor_heartbeat = None
        db.session.commit()
    client = login(app)
    client.get(f"/workflows/{workflow_id}/runs/{run_id}/status")
    # A second poll must not start a second follower.
    with app.app_context():
        assert not remote.claim(run_id)
    wait_for(app, run_id, lambda run: run.status != "running")
    with app.app_context():
        run = db.session.get(WorkflowRun, run_id)
        assert run.status == "success", run.log
        assert "Reconnected to the run on the cluster." in run.log
        assert run.log.count("Task slow completed.") == 1
        assert run.monitor_heartbeat is None


def test_the_orchestrator_can_run_as_a_slurm_job(app, cluster, tmp_path, monkeypatch):
    """Fake sbatch/squeue/scancel: sbatch runs --wrap in the background."""
    bin_dir = tmp_path / "slurm-bin"
    bin_dir.mkdir()
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (bin_dir / "sbatch").write_text(f"""#!{sys.executable}
import subprocess, sys, pathlib
args = sys.argv[1:]
wrap = args[args.index("--wrap") + 1]
output = next(a.split("=", 1)[1] for a in args if a.startswith("--output="))
chdir = next(a.split("=", 1)[1] for a in args if a.startswith("--chdir="))
pathlib.Path("{jobs}/args").write_text("\\n".join(args))
job = str(len(list(pathlib.Path("{jobs}").glob("*.pid"))) + 4242)
p = subprocess.Popen(["sh", "-c", wrap], cwd=chdir, stdout=open(output, "ab"), stderr=subprocess.STDOUT, start_new_session=True)
pathlib.Path("{jobs}/" + job + ".pid").write_text(str(p.pid))
print(job)
""")
    (bin_dir / "squeue").write_text(f"""#!{sys.executable}
import os, sys, pathlib
job = sys.argv[sys.argv.index("-j") + 1]
pid = int(pathlib.Path("{jobs}/" + job + ".pid").read_text())
import subprocess
state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
print("" if not state or state.startswith("Z") else "RUNNING")
""")
    (bin_dir / "scancel").write_text("#!/bin/sh\nexit 0\n")
    for tool in ("sbatch", "squeue", "scancel"):
        (bin_dir / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    with app.app_context():
        settings.set_value("hpc_orchestrator", "slurm")
        settings.set_value("hpc_sbatch_options", "--time=01:00:00 --partition=service")
        db.session.commit()
        user = link(cluster)
        workflow = new_workflow("SlurmOrchestrator", template="batch")
        run = start(workflow, user)
        assert run.status == "success", run.log
        assert "Waiting in the Slurm queue as job 4242." in run.log and "orchestrator on Slurm" in run.log
        sbatch_args = (jobs / "args").read_text().splitlines()
        assert "--time=01:00:00" in sbatch_args and "--partition=service" in sbatch_args and "--job-name=dagonweb-run-%d" % run.id in sbatch_args


def test_sbatch_options_are_validated(app):
    with app.app_context():
        with pytest.raises(ValueError, match="long options"):
            settings.validate(settings.DEFINITIONS["hpc_sbatch_options"], "-p debug")
        assert settings.validate(settings.DEFINITIONS["hpc_sbatch_options"], "--partition=debug --time=1:00:00")


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


# ---------------------------------------------------------------- signing in with an SSH key

needs_ssh_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen is not installed")


def ssh_key(tmp_path: Path, kind: str = "ed25519") -> tuple[Path, str]:
    path = tmp_path / f"id_{kind}"
    extra = ["-b", "3072"] if kind == "rsa" else []
    subprocess.run(["ssh-keygen", "-q", "-t", kind, *extra, "-N", "", "-C", "me@laptop", "-f", str(path)], check=True)
    return path, path.with_suffix(".pub").read_text().strip()


def ssh_sign(key: Path, message: bytes, namespace: str = sshsig.NAMESPACE) -> str:
    done = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", namespace, "-f", str(key)], input=message, capture_output=True, check=True)
    return done.stdout.decode()


@needs_ssh_keygen
@pytest.mark.parametrize("kind", ["ed25519", "ecdsa", "rsa"])
def test_openssh_signatures_are_verified(tmp_path, kind):
    key, public = ssh_key(tmp_path, kind)
    armored = ssh_sign(key, b"challenge")
    signature = sshsig.parse(armored)
    assert signature.public_key == sshsig.key_blob(public)
    sshsig.verify(signature, b"challenge")
    with pytest.raises(sshsig.SignatureError, match="does not match"):
        sshsig.verify(signature, b"another challenge")
    with pytest.raises(sshsig.SignatureError, match="-n dagonweb-login"):
        sshsig.verify(sshsig.parse(ssh_sign(key, b"challenge", "file")), b"challenge")
    with pytest.raises(sshsig.SignatureError):
        sshsig.parse(armored.replace("SIGNATURE-----\n", "SIGNATURE-----\nAAAA", 1))
    with pytest.raises(sshsig.SignatureError, match="whole signature"):
        sshsig.parse("ssh-ed25519 AAAA")


def ssh_login_setup(app, cluster, tmp_path, line: str) -> None:
    """Link the account and put ``line`` (plus the DAGonWeb key) in the runner's authorized_keys."""
    authorized = tmp_path / "authorized_keys"
    config = json.loads(cluster.config.read_text())
    cluster.config.write_text(json.dumps({**config, "authorized_keys_files": [str(authorized)]}), encoding="utf-8")
    with app.app_context():
        user = link(cluster)
        authorized.write_text(f"# my keys\n{hpc.authorized_keys_line(user)}\n{line}\n", encoding="utf-8")
        settings.set_value("hpc_ssh_login_enabled", True)
        db.session.commit()


def challenge_for(client, username: str = UNIX_USER) -> str:
    assert client.post("/login/ssh", data={"username": username}).status_code == 302
    with client.session_transaction() as session:
        return session["ssh_login_challenge"]["challenge"]


@needs_ssh_keygen
def test_users_sign_in_with_a_key_from_their_authorized_keys(app, cluster, tmp_path):
    key, public = ssh_key(tmp_path)
    ssh_login_setup(app, cluster, tmp_path, public)
    client = app.test_client()
    assert "Sign in with your cluster SSH key" in client.get("/login").get_data(as_text=True)
    challenge = challenge_for(client)
    assert UNIX_USER in challenge and "localhost" in challenge
    page = client.get("/login/ssh/sign").get_data(as_text=True)
    assert challenge in page and "ssh-keygen -Y sign -n dagonweb-login" in page
    signature = ssh_sign(key, challenge.encode())
    response = client.post("/login/ssh/sign", data={"signature": signature})
    assert response.status_code == 302 and "/login" not in response.headers["Location"]
    with client.session_transaction() as session:
        assert session.get("_user_id")
    with app.app_context():
        assert AuditLog.query.filter_by(action="auth.login", detail="sshsig").count() == 1
    # A challenge works once: it is gone from the session after any attempt.
    with client.session_transaction() as session:
        assert "ssh_login_challenge" not in session
    # An expired challenge is refused even with a good signature.
    replay = app.test_client()
    with replay.session_transaction() as session:
        session["ssh_login_challenge"] = {"username": UNIX_USER, "challenge": challenge, "expires": 0}
    assert "/login/ssh" in replay.post("/login/ssh/sign", data={"signature": signature}).headers["Location"]
    with replay.session_transaction() as session:
        assert "_user_id" not in session and "ssh_login_challenge" not in session


@needs_ssh_keygen
@pytest.mark.parametrize("case", ["restricted", "missing", "other-user", "echo"])
def test_ssh_sign_in_refusals(app, cluster, tmp_path, case):
    key, public = ssh_key(tmp_path)
    (tmp_path / "other").mkdir()
    _, other_public = ssh_key(tmp_path / "other")
    line = {"restricted": f'command="/bin/true" {public}', "missing": other_public, "other-user": public, "echo": public}[case]
    ssh_login_setup(app, cluster, tmp_path, line)
    client = app.test_client()
    username = "someone" if case == "other-user" else UNIX_USER
    challenge = challenge_for(client, username)
    message = challenge.encode() + (b"\n" if case == "echo" else b"")
    response = client.post("/login/ssh/sign", data={"signature": ssh_sign(key, message)}, follow_redirects=True)
    with client.session_transaction() as session:
        signed_in = bool(session.get("_user_id"))
    if case == "echo":  # signed with `echo`, so with a trailing newline: accepted too
        assert signed_in
        return
    assert not signed_in
    assert "does not sign in to this cluster account" in response.get_data(as_text=True)
    with app.app_context():
        assert AuditLog.query.filter_by(action="auth.login_failed").count() == 1


def test_ssh_sign_in_is_off_by_default(app):
    client = app.test_client()
    assert client.get("/login/ssh").status_code == 404
    assert "cluster SSH key" not in client.get("/login").get_data(as_text=True)
