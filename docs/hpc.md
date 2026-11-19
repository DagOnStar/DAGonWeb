# Running on an HPC cluster as the users' own accounts

DAGonWeb can run on the login node of an HPC cluster with Docker Compose and execute every workflow **as the Unix account of the user who starts it**. Files, quotas, permissions and Slurm accounting all stay the user's own. The DAGonWeb container never gets root on the host. It has no Docker socket, no `/home` mount and no access to `/etc/shadow`, and it reaches the cluster only over SSH, through a single restricted program.

```
browser ─TLS─▶ DAGonWeb container ──ssh alice@login (forced command)──▶ dagonweb-runner (runs as alice)
                │  database, UI; no host privileges                     │ DAGonStar launch()/wait()
                ◀────────────── JSON events, status, files ─────────────┘ $SCRATCH/dagonweb/run_<id>
```

## How it works

1. **Linking an account.** On **Your account → Cluster account**, a user enters their cluster user name. DAGonWeb generates an Ed25519 key pair for that DAGonWeb account and shows one line to append to `~/.ssh/authorized_keys` on the cluster:

   ```
   restrict,from="172.16.0.0/12",command="/opt/dagonweb/bin/dagonweb-runner" ssh-ed25519 AAAA... dagonweb-account-42
   ```

   Only that Unix user can write their `authorized_keys`, so when DAGonWeb signs in with the key (**Verify cluster account**), it has proved that the DAGonWeb account controls the Unix account. DAGonWeb also checks that the runner reports the same user name and a uid of at least `hpc_min_uid`. A verified cluster account can be linked to only one DAGonWeb account.
2. **What the key allows.** `restrict` disables shells, PTYs and port, agent and X11 forwarding. `command=` makes sshd start `dagonweb-runner` whatever the client asks for, and `from=` limits the key to the DAGonWeb container's network. The runner accepts a fixed set of verbs (`ping`, `submit`, `status`, `cancel`, `ls`, `get`, `delete`). It never passes anything to a shell, and it touches files only below its base directory (`$SCRATCH/dagonweb`, otherwise `~/dagonweb`).
3. **Running.** When a user presses **Run**, DAGonWeb turns the workflow into a run specification. These are the same `DagonTask` arguments the local executor builds from `app/task_specs.py`. DAGonWeb sends the specification to `dagonweb-runner submit` as the submitter's own account. The runner starts DAGonStar detached, in `run_<id>/`, with the checkpoint in `run_<id>/_dagonweb/checkpoint.json`, and writes task events to a JSON-lines file. DAGonWeb polls `status` and updates the run page, the logs and the task states exactly as it does for local runs. Slurm tasks are submitted with `sbatch` as the user.
4. **Files.** The run page browses, previews and downloads task files through `ls` and `get`, limited to the run directory. Symbolic links that point outside it are refused.
5. **Resume.** A failed run resumes from its cluster checkpoint. Only its submitter can resume it.

### Rules that keep it safe

- With `hpc_enabled` on, **nothing executes inside the DAGonWeb container**. Users must link an account before they can run anything, and the dispatcher refuses any run that has no cluster account.
- **A run always executes as the account of the person who starts it.** That holds for public workflows and for workflows owned by someone else. It also holds for administrators: they cannot run or resume anything as another user.
- Private keys are encrypted at rest with Fernet. The encryption key is `HPC_KEY_SECRET`, or a key derived from `SECRET_KEY` when it is unset. Private keys are never shown, exported or logged. Unlinking deletes the key.
- The login node's host key is pinned in the settings. DAGonWeb refuses unknown or changed host keys, so it will not send keys or workflows to an impostor.
- Linking, failed verifications, unlinking and every run, together with its Unix account, are recorded in the audit log.

## Set up

### On the login node (as root, once)

1. Install DAGonStar and the runner in a Python environment that users can read:

   ```bash
   python3 -m venv /opt/dagonweb/venv
   /opt/dagonweb/venv/bin/pip install "dagonstar[docker,kubernetes] @ git+https://github.com/DagOnStar/dagonstar.git@a7e633feab5c28ec8624a16f1c95e5499ddc93d4"
   install -d -o root -g root -m 0755 /opt/dagonweb/bin
   install -o root -g root -m 0755 hpc/dagonweb_runner.py /opt/dagonweb/bin/dagonweb-runner
   sed -i '1s|.*|#!/opt/dagonweb/venv/bin/python -I|' /opt/dagonweb/bin/dagonweb-runner
   ```

2. Optionally, create `/etc/dagonweb/runner.json` (root-owned, mode 0644):

   ```json
   {"base": "$SCRATCH/dagonweb", "min_uid": 1000}
   ```

   `base` may use `~` and environment variables. `min_uid` makes the runner refuse system accounts, even if someone adds the key to one.

3. Make sure sshd accepts public keys from the Docker network. Check `PubkeyAuthentication yes`, `AllowUsers` / `AllowGroups`, and any `Match Address` blocks. If the site uses `AuthorizedKeysFile` in a non-default location, tell users where to add the line.

4. Note the host key for the settings:

   ```bash
   cat /etc/ssh/ssh_host_ed25519_key.pub
   ```

### DAGonWeb (Docker Compose on the login node)

```bash
docker compose -f docker-compose.yml -f docker-compose.hpc.yml up --build -d
```

The overlay maps `host.docker.internal` to the host, so the container can reach the login node's sshd. It also requires `HPC_KEY_SECRET`. Put a Fernet key in `.env`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep `HPC_KEY_SECRET` out of the database and back it up separately. Without it, the stored keys cannot be decrypted, and users have to link their accounts again.

Then open **Administration → HPC cluster**:

| Setting | Meaning |
| --- | --- |
| `hpc_enabled` | Run every workflow as the submitter's linked cluster account. |
| `hpc_ssh_host`, `hpc_ssh_port` | The login node as seen from the container (default `host.docker.internal:22`). |
| `hpc_ssh_host_key` | Required. The pinned public host key (one line, with or without a leading host name). |
| `hpc_runner_command` | The forced command written into the `authorized_keys` line. |
| `hpc_source_cidr` | The `from=` restriction, for example the Docker bridge network `172.16.0.0/12`. |
| `hpc_min_uid` | The lowest uid DAGonWeb accepts when verifying an account. |
| `hpc_poll_seconds` | How often run status is read from the cluster. |

Every key can also be pinned with `DAGONWEB_<KEY>`, for example `DAGONWEB_HPC_ENABLED=true`.

### Users

1. Open **Your account → Cluster account**, enter your cluster user name and press **Create my DAGonWeb key**.
2. Sign in to the cluster as usual and append the line shown to `~/.ssh/authorized_keys`, all on one line.
3. Press **Verify cluster account**.

To revoke access at any time, delete the line from `authorized_keys`, or press **Unlink cluster account** in DAGonWeb (which deletes the key).

## Testing

- `tests/test_hpc.py` simulates sshd. It accepts only a key whose public half was added to `authorized_keys`, then starts the real runner with `SSH_ORIGINAL_COMMAND` set. It covers linking, runs as the user, resume, file access, path confinement, refusals and settings.
- `tests/test_hpc_sshd.py` starts a real, unprivileged OpenSSH `sshd` on a free localhost port. It is skipped when OpenSSH is not installed. DAGonWeb links an account through paramiko, runs a workflow and reads its files. The OpenSSH client then checks, with the same key, that a shell, a PTY and forwarding are refused, that commands chained with `;` are refused, and that a wrong host key is rejected.

## Current limits (phase 1)

- **Upload and Form tasks** cannot run on the cluster yet: they need a write verb in the runner. Table, Chart and Download tasks run, and their files are listed on the run page, but their inline previews are not shown yet.
- **ZIP archives** of a task folder and **FAIR exports** are not available for cluster runs. Download single files, or open the run folder on the cluster.
- The DAGonStar orchestrator runs **on the login node** (detached, as the user). Heavy work should use Slurm tasks. A later phase will run the orchestrator itself as a small Slurm job.
- If the web process restarts, the polling of active runs stops. The runs keep going on the cluster, but DAGonWeb shows them as running until they are resumed.
- Downloads of a single file are limited to 256 MB.
- In cluster mode, the merged `dagon.ini` (global and per-workflow) is written into the user's run folder, so keep no secrets in the global `dagon.ini` that users must not see.
- Account erasure asks the runner to delete the user's run folders. If the account is no longer reachable, the folders stay in the user's own cluster storage.
