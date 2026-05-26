# Agent onboarding (new compute environment)

For someone bringing a new compute environment into runway: an SSH-able
GPU host, a Slurm cluster, a GCP project. This is **decentralized** — each
environment runs its own `rwy-agent` process. Agents are peers; they
dedup work via a central commit-status claim lock, not via a scheduler.

If you're an intern who just wants to submit jobs, see
`docs/onboarding-intern.md`.

> **Timestamps.** Everything in this system — run_ids, agent log lines,
> commit status timestamps, issue comment updates — is **UTC, ISO 8601**
> with a trailing `Z`. The only place local time shows up is your shell
> prompt. Don't try to match `date +%Y%m%d` against a run_id near
> midnight UTC; use `date -u +%Y%m%d` instead.

---

## The model in one paragraph

`rwy-agent` is a polling daemon. It watches `runway-lab/runway-jobs/main`
for newly-merged job specs, claims the ones whose `spec.backends` contains
the backend it serves (`ssh` / `slurm` / `gcp`), pulls the user's
encrypted secret from `runway-lab/runway-secrets`, decrypts it with the
agent host's age private key, and executes the spec. State updates go
back as commit statuses + issue comments on `runway-jobs`. **Each agent
host has its own age key**; the intern's secret file is encrypted to all
agent recipients in `runway-secrets/recipients/agents/*.age.pub`, so any
agent serving the requested backend can decrypt and run.

---

## Common steps (every backend)

These run on the **agent host** — the machine that will execute jobs.
For Slurm, this is the head/login node. For SSH, it's the GPU box. For
GCP, a control VM with `gcloud` access.

### 1. Install prerequisites

- Python 3.10 or newer
- `git` (for cloning user experiments + the secrets repo)
- `age` and `age-keygen` (FiloSottile/age 1.1+) on `$PATH`
- The `gh` CLI authenticated as a service account, or a `GITHUB_TOKEN`
  env var with `repo` scope on `runway-lab/runway-jobs` and read access
  on `runway-lab/runway-secrets`
- Outbound HTTPS to `github.com` and `api.github.com`

### 2. Install `runway-tools`

```bash
pip install --user "git+ssh://git@github.com/runway-lab/runway-tools.git"
```

(Private repo; needs SSH access to the org or a HTTPS token.)

### 3. Generate the agent's age keypair

```bash
mkdir -p ~/.config/age
age-keygen -o ~/.config/age/agent.key
chmod 600 ~/.config/age/agent.key
grep '^# public key:' ~/.config/age/agent.key
```

The public key is what other people use to encrypt to this agent.
Keep the private key on the host — it never leaves.

### 4. Register the agent's pubkey

Open a PR to `runway-lab/runway-secrets` adding
`recipients/agents/<agent-name>.age.pub` (the file's body is the public
key from step 3). Naming convention:

| Backend | Suggested name |
|---|---|
| SSH host | `<hostname>` (e.g. `4vita2`) |
| Slurm cluster | `slurm-<labname>` (e.g. `slurm-mit-csail`) |
| GCP project | `gcp-<project-id>` |

The repo's auto-merge gate rejects PRs that change anyone else's
`recipients/`, but the operator can `--admin` merge for new agent
recipients.

> ⚠️ Existing interns must re-run `rwy register wandb` and `rwy register
> hf` after a new agent is added — their secrets are re-encrypted to
> include the new recipient. Until they do, **this agent cannot decrypt
> their secrets**. The agent will skip the run with a clear error, not
> silently fail.

### 5. Clone runway-secrets locally for the agent to pull from

```bash
git clone https://github.com/runway-lab/runway-secrets.git ~/.runway-secrets
```

The agent does `git pull` on this clone every cycle. Keep it
read-only on disk; the agent never writes here.

### 6. Run the agent

See backend-specific sections below for the exact flags. All backends
share these flags:

```bash
rwy-agent run \
  --backend <ssh|slurm|gcp> \
  --workspace ~/.rwy/work \
  --code-cache ~/.rwy/code-cache \
  --state-db ~/.rwy/state.db \
  --secrets-repo ~/.runway-secrets \
  --age-key ~/.config/age/agent.key \
  --agent-id <stable-unique-id> \
  --interval 60
```

`--agent-id` should be globally unique across all running agents (the
central claim lock uses it as the tiebreak in races). Convention:
`<backend>-<host-or-cluster>-<n>`, e.g. `slurm-mit-csail-1`,
`ssh-4vita2`, `gcp-runway-prod-1`.

### 7. Process supervision

`rwy-agent` is a long-running process; if it dies, the cluster goes
offline. Pick one:

- `nohup rwy-agent run ... &> ~/.rwy/agent.log &` — simplest, dies on
  reboot.
- systemd user unit — survives reboot if `loginctl enable-linger` is set.
- `tmux new-session -d -s rwy 'rwy-agent run ...'` — survives logout if
  the tmux server keeps running.

**Avoid `tmux` on shared head nodes** — we've seen the tmux server get
killed by node restarts (`runway-multi-agent-race` was caused by exactly
this). Prefer systemd or `nohup`.

---

## Backend: SSH

For a single GPU host (e.g. a lab workstation or a cloud VM you SSH into).
The agent and the workload run on the same machine.

```bash
rwy-agent run --backend ssh \
  --workspace ~/.rwy/work \
  --code-cache ~/.rwy/code-cache \
  --agent-id ssh-<hostname> \
  ...
```

What it does: clones `spec.code.repo` into the code cache, runs
`spec.run` as a bash subprocess (`bash` inherits env including
GITHUB_TOKEN + decrypted secrets), parses stdout for progress JSON and
`<NAME>_URL=` lines.

No special requirements beyond the common ones.

---

## Backend: Slurm

For a Slurm cluster. The agent runs on the head/login node and submits
to local Slurm via `sbatch`; the **compute nodes** actually run the
workload. There is no SSH layer between the agent and Slurm.

### Requirements

The head/login node must have:

1. **Slurm client commands** on `$PATH`: `sbatch`, `sacct`, `scancel`,
   and (optionally) `squeue`. The agent only calls `sbatch --parsable`
   and `sacct --noheader --parsable2 --format=State`.
2. **A Slurm account / partition the operator can submit to.** If
   submission requires `--account=lab-runway` or `--partition=gpu`,
   pass them via `--slurm-account` and `--slurm-partition`.
3. **A shared filesystem mounted on head + compute nodes.** The agent's
   `--workspace` directory must be readable by compute nodes (Slurm
   writes `slurm-<jobid>.out` there). Common locations: `/scratch/<user>`,
   `/work/<user>`. Avoid `~` if home is not mounted on compute nodes.
4. **Compute nodes with outbound HTTPS to github.com** — the sbatch
   script does `git clone https://x-access-token:$GITHUB_TOKEN@…` from
   the compute node, not the head node. If your compute nodes are
   air-gapped, this won't work and you'll need to pre-stage code (see
   caveat below).
5. **Default `--export=ALL`** must be honored by your Slurm config —
   we propagate decrypted secrets (and `GITHUB_TOKEN`) through the
   sbatch parent process's environment. Some sites override with
   `--export=NONE` in `slurm.conf`; check with `scontrol show config |
   grep PropagateResourceLimits`.
6. **`#SBATCH --time` ceiling.** The agent translates
   `spec.resources.max_hours` (float, in hours) into `HH:MM:SS`. If
   your partition's `MaxTime` is shorter than the spec requests,
   `sbatch` will reject the job and the agent will mark the run
   `failed (rc=1)` with sbatch's stderr in the issue comment.
7. **GPU resource string.** The agent emits `#SBATCH --gres=gpu:<count>`
   when `spec.resources.gpu_type == "any"`, or
   `#SBATCH --gres=gpu:<type>:<count>` when a specific type is given.
   The type names (`a100`, `h100`, `v100`, …) must match what
   `scontrol show node` reports as `Gres`.

### Caveats

These are known limitations the operator should plan around:

1. **No orphan-job recovery on agent restart.** If `rwy-agent` is
   killed mid-poll while a Slurm job is RUNNING, the job keeps running
   on Slurm but the agent forgets about it on restart. The commit
   status stays in `pending` forever and no one updates the issue
   comment. **Mitigation today:** wait for the job to drain before
   restarting; or `scancel` it manually before restart.
   *Roadmap:* persist `(run_id → job_id, sha)` to `state.db` and
   resume polling on startup.
2. **No automatic partition selection.** The agent uses whatever
   `--slurm-partition` you start it with. If the partition fills up,
   jobs queue indefinitely; the agent will not failover to another
   partition. Run multiple agents on different partitions with
   different `--agent-id` if you need that.
3. **Secrets in sbatch env are visible to anyone with `scontrol show
   job` privilege.** Slurm doesn't redact env from job inspection.
   If your cluster shares head nodes across labs, decide whether
   `WANDB_API_KEY` / `HF_TOKEN` visibility is acceptable; if not,
   talk to the operator before onboarding.
4. **Workdir layout is one directory per `run_id` under
   `--workspace`.** No automatic cleanup. Set up a cron `find
   ~/.rwy/work -mindepth 1 -maxdepth 1 -mtime +14 -exec rm -rf {} +`
   or it will fill the FS over time.
5. **`sacct` lag.** On busy clusters `sacct` can take 30-60s to
   reflect a state transition. The agent treats absence-of-row as
   `PENDING` and retries — fine for most cases, but if your cluster
   is very slow, the agent reports the run as still pending after
   the job actually started. Bump `--slurm-poll-interval` to 60+ to
   reduce noise.
6. **Compute-node clone failures are reported as run failures.** If
   GitHub is briefly down or the token expires mid-job, the user
   sees `git clone failed` in their issue comment — not an
   infrastructure problem the agent retries. Re-submit by changing
   `spec.code.ref` (forces a new run_id) or wait for the spec
   author to do so.
7. **Multiple Slurm agents on the same cluster** are allowed but
   compete for every spec — the central claim lock picks one
   deterministically. There's no benefit to running more than one
   unless you want failover; for that, use `nohup` + a watchdog
   instead.

### Step-by-step (Slurm onboarding)

After the common steps 1-5 above:

```bash
# Confirm Slurm tools and find a workable partition.
which sbatch sacct scancel
sinfo -o '%P %a %l %D %F'   # list partitions and time limits
scontrol show config | grep -E 'MaxTime|Partition'

# Pick a workspace on a shared filesystem.
mkdir -p /scratch/$USER/rwy/{work,code-cache}
mkdir -p ~/.rwy && ln -sf /scratch/$USER/rwy ~/.rwy/shared

# Start the agent.
nohup rwy-agent run \
  --backend slurm \
  --workspace /scratch/$USER/rwy/work \
  --code-cache /scratch/$USER/rwy/code-cache \
  --state-db ~/.rwy/state.db \
  --secrets-repo ~/.runway-secrets \
  --age-key ~/.config/age/agent.key \
  --agent-id slurm-<labname>-1 \
  --slurm-partition gpu \
  --slurm-account <lab-account-if-any> \
  --slurm-poll-interval 30 \
  --interval 60 \
  > ~/.rwy/agent.log 2>&1 &
```

### Smoke test

The operator (admin) or any intern submits a tiny spec with
`backends: [slurm]`:

```yaml
apiVersion: runway/v1alpha1
kind: Experiment
metadata: {name: slurm-smoke, owner: <github-login>}
spec:
  code: {repo: runway-lab/<you>-experiments, ref: <sha>}
  resources: {gpus: 0, gpu_type: any, max_hours: 1}
  backends: [slurm]
  selection: {policy: eta, profile_seconds: 0}
  run: |
    set -e
    echo "hello from slurm compute node"
    hostname
    echo '{"step": 1, "total_steps": 1}'
  artifacts: {uri: "gs://${ARTIFACTS_BUCKET}/runs/{run_id}/candidates/{backend_id}/"}
```

Submit via `rwy submit /tmp/slurm-smoke.yaml`. Within ~2 minutes you
should see:

1. PR auto-merged on `runway-jobs`.
2. Tracking issue `[run] slurm-smoke — <run_id>` opened with labels
   `run`, `status:running`, `backend:slurm`.
3. Commit status `agent:slurm` flipping `pending → success` on the
   merge commit.
4. The agent log shows `submitted slurm job <jobid>` and later
   `slurm <jobid> state=COMPLETED`.
5. `sacct -j <jobid>` on the head node shows the run.
6. `~/.rwy/work/<run_id>/slurm-<jobid>.out` exists and contains
   "hello from slurm compute node" + the compute node's hostname.

If any of 1-4 fail, `tail -100 ~/.rwy/agent.log` and the issue
comment have the diagnostic.

---

## Backend: GCP

The GCP backend runs from a control machine that already has working
GCP/SkyPilot credentials. It submits each spec as a SkyPilot managed job
(`sky jobs launch --detach-run`) and polls `sky jobs queue` / `sky jobs logs`
until the managed job reaches a terminal state.

### Prerequisites

```bash
gcloud auth list
gcloud config get-value project
sky check gcp
sky jobs queue --all --output json
```

If `sky jobs queue` says the managed-jobs controller is not up yet, that is
not fatal for onboarding; the first `sky jobs launch` will create/start it.
Quota or permission errors must be fixed before starting the agent.

Use a stable project-specific agent id and recipient name:

| GCP project | Suggested agent name |
|---|---|
| `snap-umap-dev` | `gcp-snap-umap-dev` |

Register the agent's age public key in `runway-secrets` as
`recipients/agents/gcp-<project-id>.age.pub`, then ask existing interns to
re-run `rwy register wandb` / `rwy register hf` so their encrypted secrets add
this new recipient.

### Launch

```bash
mkdir -p ~/.rwy/gcp

GITHUB_TOKEN="$(gh auth token)" \
nohup rwy-agent run \
  --backend gcp \
  --workspace ~/.rwy/gcp/work \
  --code-cache ~/.rwy/gcp/code-cache \
  --state-db ~/.rwy/gcp/state.db \
  --secrets-repo ~/.runway-secrets \
  --age-key ~/.config/age/agent.key \
  --agent-id gcp-snap-umap-dev-1 \
  --gcp-infra gcp/us-east5 \
  --gcp-poll-interval 60 \
  --interval 60 \
  >> ~/.rwy/gcp-agent.log 2>&1 &
```

`--gcp-infra` is passed directly to SkyPilot. Use `gcp` to let SkyPilot choose
within GCP, `gcp/<region>` to constrain region, or
`gcp/<region>/<zone>` to constrain a zone. The agent reads
`spec.resources.gpu_type` and `spec.resources.gpus` and passes them as
`--gpus <gpu_type>:<count>`.

Current policy caps jobs at 4 GPUs, so normal `A100`, `A100-80GB`, and `H100`
specs can pass validation. `H100-MEGA` maps to GCP A3 Mega and is fixed at 8
GPUs, so it will not pass until `policies/default.yaml` raises
`max_gpus_per_job` to 8.

### Smoke test

Submit a very short spec with `backends: [gcp]`, a pinned code SHA, and a
smallest-available allowed GPU shape, e.g. `resources: {gpus: 1,
gpu_type: H100, max_hours: 1}`. The `run:` block should print one progress
line and exit:

```yaml
apiVersion: runway/v1alpha1
kind: Experiment
metadata: {name: gcp-smoke, owner: <github-login>}
spec:
  code: {repo: runway-lab/<you>-experiments, ref: <sha>}
  resources: {gpus: 1, gpu_type: H100, max_hours: 1}
  backends: [gcp]
  selection: {policy: eta, profile_seconds: 0}
  run: |
    set -e
    echo "hello from gcp"
    hostname
    echo '{"step": 1, "total_steps": 1}'
  artifacts: {uri: "gs://${ARTIFACTS_BUCKET}/runs/{run_id}/candidates/{backend_id}/"}
```

Expected signals:

1. Tracking issue has `backend:gcp` and an `agent:gcp` live comment.
2. Commit status `agent:gcp` moves `pending → success`.
3. `tail -100 ~/.rwy/gcp-agent.log` shows `submitted SkyPilot managed job`.
4. `sky jobs queue --all` shows the corresponding `rwy-<run_id>` job.
5. The SkyPilot worker terminates after success; managed jobs clean up the
   run cluster.

---

## Verifying the agent is healthy

```bash
# Process alive?
pgrep -af rwy-agent

# Pulling secrets and decoding?
tail -50 ~/.rwy/agent.log | grep -iE 'decrypt|claim|sbatch|status'

# Last successful run?
gh issue list --repo runway-lab/runway-jobs --label backend:slurm \
  --search 'is:closed' --limit 5

# Is this agent's pubkey in every intern's secret?
# (Run on a host where you have the matching private key — or just
# check that interns re-ran `rwy register` after the agent's PR merged.)
gh api repos/runway-lab/runway-secrets/contents/secrets \
  --jq '.[].name'    # list secret files
# Each file should encrypt to all currently-registered agents.
```

---

## Removing an agent

1. Stop the agent process (`pkill rwy-agent`).
2. Open a PR to `runway-lab/runway-secrets` removing
   `recipients/agents/<agent-name>.age.pub`.
3. Existing intern secrets are still encrypted to the removed key, but
   that key is no longer published. New `rwy register` runs will not
   include the dead recipient. Old ciphertexts remain decryptable by
   the dead key — destroy the agent's `~/.config/age/agent.key` to
   make this irrecoverable.

---

## Where to look next

- **Intern flow**: `docs/onboarding-intern.md`
- **Day-to-day**: `docs/routine.md`
- **GitHub permission model + admin setup**: `docs/github-setup.md`
- **Architectural decisions**: `docs/discussions.md`
