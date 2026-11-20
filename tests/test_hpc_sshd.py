"""HPC cluster mode against a real OpenSSH server (skipped when sshd is missing).

An unprivileged ``sshd`` listens on a free localhost port, with its own host
key and an ``authorized_keys`` file that holds exactly the line the account
page shows. DAGonWeb then links the account, runs a workflow and reads its
files through paramiko, and the OpenSSH client checks that the key cannot do
anything but start ``dagonweb-runner``.
"""
from __future__ import annotations

import getpass
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import create_app, hpc, hpc_check, seed_defaults, settings
from app.config import TestConfig
from app.executor.dispatch import run_job
from app.extensions import db
from app.models import Setting, User, Workflow, WorkflowRun
from app.workflows.routes import apply_workflow_template

SSHD = shutil.which("sshd") or next((path for path in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if Path(path).is_file()), None)
SSH = shutil.which("ssh")
RUNNER = Path(__file__).resolve().parent.parent / "hpc" / "dagonweb_runner.py"
pytestmark = pytest.mark.skipif(not (SSHD and SSH and shutil.which("ssh-keygen")), reason="OpenSSH server and client are not installed")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def sshd(tmp_path):
    root = tmp_path / "sshd"
    (root / "bin").mkdir(parents=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "host_ed25519")], check=True)
    (root / "authorized_keys").touch(mode=0o600)
    # authorized_keys_files matches AuthorizedKeysFile below, as docs/hpc.md asks sites to do.
    (root / "runner.json").write_text(json.dumps({"base": str(root / "cluster"), "min_uid": 0, "authorized_keys_files": [str(root / "authorized_keys")]}), encoding="utf-8")
    runner = root / "bin" / "dagonweb-runner"
    runner.write_text(f'#!/bin/sh\nDAGONWEB_RUNNER_CONFIG={root / "runner.json"} exec {sys.executable} -I {RUNNER} "$@"\n', encoding="utf-8")
    runner.chmod(0o755)
    port = free_port()
    (root / "sshd_config").write_text("\n".join([
        f"Port {port}", "ListenAddress 127.0.0.1", f"HostKey {root / 'host_ed25519'}", f"PidFile {root / 'sshd.pid'}",
        f"AuthorizedKeysFile {root / 'authorized_keys'}", "PubkeyAuthentication yes", "PasswordAuthentication no",
        "KbdInteractiveAuthentication no", "UsePAM no", "StrictModes no", f"AllowUsers {getpass.getuser()}",
    ]) + "\n", encoding="utf-8")
    process = subprocess.Popen([SSHD, "-D", "-e", "-f", str(root / "sshd_config")], stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if process.poll() is not None:
                pytest.skip("sshd cannot run unprivileged here")
            time.sleep(0.1)
    yield {"root": root, "port": port, "runner": runner, "host_key": (root / "host_ed25519.pub").read_text().strip()}
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture()
def app(tmp_path, sshd):
    application = create_app(TestConfig)
    application.config["DAGON_INI_PATH"] = str(tmp_path / "dagon.ini")
    with application.app_context():
        db.create_all()
        seed_defaults(application)
        Setting.query.filter_by(key="scratch_dir").one().value = str(tmp_path / "scratch")
        for key, value in {"hpc_enabled": True, "hpc_ssh_host": "127.0.0.1", "hpc_ssh_port": sshd["port"], "hpc_ssh_host_key": sshd["host_key"],
                           "hpc_runner_command": str(sshd["runner"]), "hpc_source_cidr": "127.0.0.1/32", "hpc_min_uid": 0, "hpc_poll_seconds": 0.3}.items():
            settings.set_value(key, value)
        db.session.commit()
    return application


def test_link_run_and_files_over_real_ssh(app, sshd, tmp_path):
    with app.app_context():
        user = User.query.filter_by(email="admin@example.org").one()
        hpc.start_link(user, getpass.getuser())
        with pytest.raises(hpc.HpcError, match="refused the key"):
            hpc.verify_link(user)
        (sshd["root"] / "authorized_keys").write_text(hpc.authorized_keys_line(user) + "\n", encoding="utf-8")
        hpc.verify_link(user)
        db.session.commit()
        assert user.unix_linked

        workflow = Workflow(owner_id=user.id, name="RealSSH")
        db.session.add(workflow)
        db.session.flush()
        apply_workflow_template(workflow, "batch")
        run = WorkflowRun(workflow_id=workflow.id, user_id=user.id, status="pending", scratch_path="", log="", unix_username=user.unix_username)
        db.session.add(run)
        db.session.commit()
        run_job(workflow.id, run.id)
        db.session.expire_all()
        run = db.session.get(WorkflowRun, run.id)
        assert run.status == "success", run.log
        merge = next(task_run for task_run in run.task_runs if task_run.task_uid == "merge")
        merge_id, user_id = merge.id, user.id
        assert not (tmp_path / "scratch" / f"workflow-{workflow.id}").exists()

        # Wrong host key: refused before authenticating.
        settings.set_value("hpc_ssh_host_key", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl")
        db.session.commit()
        with pytest.raises(hpc.HpcError, match="different host key"), hpc.client_for(user) as client:
            client.ping()
        settings.set_value("hpc_ssh_host_key", sshd["host_key"])
        db.session.commit()
        key = tmp_path / "client_key"
        key.write_text(hpc.decrypt_private_key(user.unix_private_key), encoding="utf-8")
        key.chmod(0o600)

    web = app.test_client()
    with web.session_transaction() as session:
        session["_user_id"], session["_fresh"] = str(user_id), True
    assert "result.txt" in {item["name"] for item in web.get(f"/workflows/task-runs/{merge_id}/files").get_json()["files"]}
    assert web.get(f"/workflows/task-runs/{merge_id}/download?path=result.txt").data == b"HELLO\nolleh\n"

    # The OpenSSH client with the same key: no shell, no PTY, no forwarding, no sftp.
    known = tmp_path / "known_hosts"
    known.write_text(f"[127.0.0.1]:{sshd['port']} {' '.join(sshd['host_key'].split()[:2])}\n", encoding="utf-8")
    base = [SSH, "-F", "/dev/null", "-p", str(sshd["port"]), "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={known}", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", f"{getpass.getuser()}@127.0.0.1"]
    marker = tmp_path / "pwned"
    shell = subprocess.run(base + [f"ping; touch {marker}"], capture_output=True, timeout=30)
    assert shell.returncode != 0 and b"Unknown command" in shell.stdout and not marker.exists()
    pty = subprocess.run(base[:1] + ["-tt"] + base[1:], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    assert b"PTY allocation request failed" in pty.stderr
    forward = subprocess.run(base[:1] + ["-N", "-o", "ExitOnForwardFailure=yes", "-R", f"{free_port()}:127.0.0.1:22"] + base[1:], capture_output=True, timeout=30)
    assert forward.returncode != 0 and b"forwarding failed" in forward.stderr
    allowed = subprocess.run(base + ["ping"], capture_output=True, timeout=30)
    assert json.loads(allowed.stdout)["user"] == getpass.getuser()


def test_site_check_against_real_sshd(app, sshd):
    with app.app_context():
        assert hpc_check.probe_host_key("127.0.0.1", sshd["port"], sshd["host_key"])[0]
        wrong = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
        ok, detail = hpc_check.probe_host_key("127.0.0.1", sshd["port"], wrong)
        assert not ok and "different host key" in detail
        user = User.query.filter_by(email="admin@example.org").one()
        hpc.start_link(user, getpass.getuser())
        (sshd["root"] / "authorized_keys").write_text(hpc.authorized_keys_line(user) + "\n", encoding="utf-8")
        hpc.verify_link(user)
        db.session.commit()
    result = app.test_cli_runner().invoke(args=["hpc-check", "--email", "admin@example.org", "--run"])
    assert result.exit_code == 0, result.output
    assert "[  ok] A test workflow runs on the cluster as the user" in result.output
    assert "[  ok] The runner reads the user's authorized_keys (1 key(s))" in result.output
