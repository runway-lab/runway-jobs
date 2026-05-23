# GCP agent onboarding (design + plan)

**Status:** Design document and operator plan. **`GcpBackend` is not yet
implemented** — `rwy-agent --backend gcp` will refuse to start until
runway-tools ships the implementation (tracked: future PR). The
infrastructure side (project, service accounts, container, VM, bucket) is
still useful to set up in parallel since it's reusable.

This doc covers:

1. Architectural decisions specific to GCP (vs. `ssh` / `slurm`).
2. **Compliance gate**: ownership / cost questions to resolve before
   touching anything (this is the biggest risk on GCP, not technical).
3. Variables to nail down before onboarding.
4. Step-by-step for the human operator (with Codex-on-work-laptop notes).
5. GCP-specific quirks predicted from the design — to be confirmed /
   amended by a postmortem after the first end-to-end run, the way
   `onboarding-slurm-tacc-vista.md` documented Vista.

The cross-backend operator pattern (decentralized agents, age recipients,
secrets re-encryption, smoke test shape) is in `onboarding-agent.md`. Read
that first; this doc layers on top.

---

## 0. Compliance gate — read before doing anything else

GCP differs from Slurm/SSH in one critical respect: **every job costs
real money on a real billing account**. Before onboarding a GCP project,
the operator must answer:

1. **Who owns the GCP project's billing?**
   - **Personal billing** (your own GCP account, your own card): fine to
     onboard. You bear the cost, you bear the risk.
   - **Employer / institution billing**: you need **explicit written
     approval** from whoever owns the budget. `runway-lab` is a public
     org and intern-submitted `spec.run` is arbitrary shell — interns
     can submit GPU jobs that bill your employer. Without approval this
     is a policy violation in most organizations.

2. **Is there a per-job and per-day cost ceiling?** Vertex AI Custom
   Jobs default to a 7-day timeout. A single misconfigured `a2-megagpu-16g`
   job can burn $1000+/day. Required guards before onboarding:
   - Per-spec `max_hours` enforced (already in spec schema, but the
     `GcpBackend` must pass it as `scheduling.timeout` to Vertex).
   - Billing alert: `gcloud alpha billing budgets create` with email
     trigger at e.g. $50/day, $500/month for this project.
   - Project-level "kill switch": be ready to disable the billing
     account if a runaway happens.

3. **Who has IAM on the project?** Anyone with `roles/aiplatform.user`
   in this project can submit jobs that burn its budget. Restrict to
   the agent's service account + you. Do NOT grant interns IAM — they
   submit through `runway-jobs` PRs, not `gcloud`.

If any of (1)-(3) is unresolved, stop. Documenting "we'll figure it out
later" on a billable cloud is how surprise bills happen.

---

## 1. Architectural decisions

GCP differs enough from Slurm/SSH that the operator should understand
these choices before reading the step-by-step.

### Where the agent runs

A **small dedicated GCE VM** (e2-small or e2-medium, ~$15-25/month).
**Not** on the work laptop:

- Laptops sleep / VPN-disconnect / get reimaged. The agent must run 24/7.
- The agent needs to reach the GCP control plane non-interactively; the
  cleanest auth path is **GCE instance metadata** (the VM has an attached
  service account and inherits its credentials with zero JSON keys on
  disk).
- One-time bootstrap from the laptop, then laptop is out of the loop.

### What "compute" means

**Vertex AI Custom Jobs**. Reasons over the alternatives:

| Option | Why not |
|---|---|
| GCE batch (spin up VM per run) | Hand-roll lifecycle, no native logging integration, slower startup |
| GKE batch jobs | Requires existing cluster + k8s expertise; overkill for this scale |
| Cloud Run jobs | Doesn't support GPU at GA; CPU-only is fine but we need GPU |
| **Vertex AI Custom Jobs** | Native GPU machine types, automatic GCS artifact paths via `AIP_*` env vars, integrated with Cloud Logging, per-second billing |

### How the user's code reaches Vertex AI

Same pattern as `SlurmBackend`: **a generic runtime container clones
`spec.code.repo` at startup**, then exec's `spec.run`. No per-run
Cloud Build (slow, expensive at scale).

Container shape (to be built — `runway-runtime` image in Artifact
Registry):

```dockerfile
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y python3 python3-pip git && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

Where `entrypoint.sh` reads `RWY_CODE_REPO`, `RWY_CODE_REF`,
`RWY_RUN_SCRIPT` (the spec.run script body, passed as a base64-encoded
env var to keep it out of CLI args / argv), clones, checks out, runs
the script. The same env vars the `SlurmBackend` script uses
(`WANDB_API_KEY`, `HF_TOKEN`, `GITHUB_TOKEN`, `RWY_RUN_ID`,
`RWY_BACKEND=gcp`) are passed to the Vertex CustomJob via its env
spec — they reach the container as ordinary env vars.

### How the agent talks to Vertex

Use the **`google-cloud-aiplatform`** Python SDK. This is the only new
runtime dep `GcpBackend` introduces. `gcloud` CLI is only for the
initial human bootstrap.

### Spec → Vertex CustomJob mapping

Drafted; subject to revision when actually implemented:

| Spec field | Vertex field |
|---|---|
| `metadata.name` + `run_id` | `displayName` (truncate to 128 chars) |
| `spec.resources.gpus` + `spec.resources.gpu_type` | `workerPoolSpecs[0].machineSpec.machineType` + `acceleratorType` + `acceleratorCount` |
| `spec.resources.max_hours` | `scheduling.timeout` (seconds) |
| `spec.code.repo` / `spec.code.ref` | `workerPoolSpecs[0].containerSpec.env: [{name: RWY_CODE_REPO, value: ...}]` |
| `spec.run` | base64-encoded into `RWY_RUN_SCRIPT` env var |
| `spec.artifacts.uri` | `baseOutputDirectory.outputUriPrefix` (placeholder-expanded) |

GPU machine-type lookup table (lives in
`runway-tools/src/rwy_agent/providers.py` once implemented):

| `gpu_type` | machineType | acceleratorType |
|---|---|---|
| `t4` | `n1-standard-8` | `NVIDIA_TESLA_T4` |
| `v100` | `n1-standard-8` | `NVIDIA_TESLA_V100` |
| `a100` | `a2-highgpu-1g` (×count via `Ng`) | `NVIDIA_TESLA_A100` |
| `a100-80gb` | `a2-ultragpu-1g` | `NVIDIA_A100_80GB` |
| `h100` | `a3-highgpu-1g` | `NVIDIA_H100_80GB` |
| `l4` | `g2-standard-8` | `NVIDIA_L4` |

`gpu_type: any` is **not supported on GCP** — unlike Slurm's "give me
whatever GPU is in this partition," Vertex requires explicit machine
type. `GcpBackend` rejects `any` at spec parse with a clear error
("specify a concrete gpu_type for backend gcp").

---

## 2. Variables to nail down before starting

| Variable | Resolved by | Example |
|---|---|---|
| `PROJECT_ID` | you | `runway-lab-gcp-prod` |
| `REGION` | you (where you want GPUs) | `us-central1` |
| `ZONE` (for the agent VM) | you | `us-central1-a` |
| `AGENT_SA_EMAIL` | you create | `rwy-agent@${PROJECT_ID}.iam.gserviceaccount.com` |
| `JOB_SA_EMAIL` | you create | `rwy-job@${PROJECT_ID}.iam.gserviceaccount.com` |
| `ARTIFACTS_BUCKET` | you create | `gs://runway-${PROJECT_ID}-artifacts` |
| `RUNTIME_IMAGE` | you build | `${REGION}-docker.pkg.dev/${PROJECT_ID}/runway/runtime:v1` |
| `GPU_TYPES_AVAILABLE` | you confirm via quota | `a100`, `l4` |
| `BUDGET_DAILY_USD` | you decide | `50` |
| `agent-id` | naming convention | `gcp-${PROJECT_ID}-1` |

Confirm GPU quota **before** you create anything:

```bash
gcloud compute regions describe ${REGION} --format=json \
  | jq '.quotas[] | select(.metric | contains("NVIDIA"))'
```

Each accelerator metric has a `limit` and `usage`. If `limit` is 0 for
the GPU type you want, request quota via Console → IAM & Admin →
Quotas. This can take 1-3 business days to be approved.

---

## 3. Prerequisites (work laptop, one-time)

The laptop needs:

- `gcloud` CLI ≥ 470 (`brew install google-cloud-sdk` or
  <https://cloud.google.com/sdk/docs/install>).
- A user identity authenticated to the GCP project as owner or with
  enough permissions to create service accounts + enable APIs.
- The same prerequisites from `onboarding-agent.md` (GitHub access,
  `age`, etc.) — but you only need these to *bootstrap*; the agent VM
  is what actually carries them long-term.

**Critical pre-Codex step (human-only)**:

```bash
gcloud auth login          # browser OAuth — may go through company SSO
gcloud config set project ${PROJECT_ID}
gcloud auth application-default login   # also browser; needed for SDK
```

`gcloud auth login` opens a browser. Codex cannot drive a browser, so
the operator must do this manually **before** handing the session over
to Codex. Same for `application-default login` if the laptop will
exercise the SDK at all (the agent VM uses metadata SA, so the laptop's
ADC is only for one-off testing).

---

## 4. Step-by-step

The operator (or Codex running on the operator's laptop after step 3)
executes these in order. Each step is idempotent — re-running on a
half-finished onboarding picks up where it left off.

### 4.1. Enable APIs

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  compute.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  logging.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com
```

### 4.2. Create the agent's service account

```bash
gcloud iam service-accounts create rwy-agent \
  --display-name="Runway agent (decrypts secrets, submits Vertex jobs)"

AGENT_SA="rwy-agent@${PROJECT_ID}.iam.gserviceaccount.com"

# Permissions: submit Vertex jobs, read logs, write to artifacts bucket,
# create child SA tokens for the job-runtime SA.
for role in \
  roles/aiplatform.user \
  roles/logging.viewer \
  roles/storage.objectAdmin \
  roles/iam.serviceAccountTokenCreator
do
  gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${AGENT_SA}" --role=$role
done
```

### 4.3. Create the job-runtime service account

This is what the Vertex CustomJob runs *as* — separate from the agent
SA so a compromised training script can't submit new jobs (only the
agent SA has `aiplatform.user`).

```bash
gcloud iam service-accounts create rwy-job \
  --display-name="Runway training-job runtime"

JOB_SA="rwy-job@${PROJECT_ID}.iam.gserviceaccount.com"

# Job only needs to write to GCS (for AIP_MODEL_DIR / artifacts).
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${JOB_SA}" --role=roles/storage.objectAdmin

# Allow the agent SA to impersonate the job SA when submitting jobs.
gcloud iam service-accounts add-iam-policy-binding ${JOB_SA} \
  --member="serviceAccount:${AGENT_SA}" \
  --role=roles/iam.serviceAccountUser
```

### 4.4. Create the artifacts bucket

```bash
gcloud storage buckets create gs://runway-${PROJECT_ID}-artifacts \
  --location=${REGION} \
  --uniform-bucket-level-access
```

### 4.5. Build and push the runtime container

(Source for the Dockerfile lives in `runway-tools/runtime/gcp/` once
the `GcpBackend` PR lands. Until then, this step waits.)

```bash
# Create the artifact registry repo
gcloud artifacts repositories create runway \
  --repository-format=docker --location=${REGION}

# Build + push via Cloud Build (uses Cloud Build's default SA, which
# already has Artifact Registry write).
cd runway-tools/runtime/gcp
gcloud builds submit \
  --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/runway/runtime:v1 .
```

### 4.6. Set up billing alert

```bash
# Find the billing account
BILLING_ACCOUNT=$(gcloud billing projects describe ${PROJECT_ID} \
  --format='value(billingAccountName)' | cut -d/ -f2)

# Create a $50/day alert (adjust to taste)
gcloud billing budgets create \
  --billing-account=${BILLING_ACCOUNT} \
  --display-name="runway-${PROJECT_ID}-daily" \
  --budget-amount=50USD \
  --filter-projects=projects/${PROJECT_ID} \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=1.0 \
  --threshold-rule=percent=1.5
```

### 4.7. Create the agent VM

```bash
gcloud compute instances create rwy-agent-${PROJECT_ID} \
  --zone=${ZONE} \
  --machine-type=e2-small \
  --service-account=${AGENT_SA} \
  --scopes=cloud-platform \
  --image-family=debian-12 --image-project=debian-cloud \
  --tags=rwy-agent \
  --metadata-from-file=startup-script=<(cat <<'STARTUP'
#!/usr/bin/env bash
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git age tmux jq
STARTUP
)
```

### 4.8. SSH into the agent VM and finish setup

From this point on, the laptop only opens an SSH session; everything
else runs on the VM. **Codex on the laptop can drive this via
`gcloud compute ssh` invocations**.

```bash
gcloud compute ssh rwy-agent-${PROJECT_ID} --zone=${ZONE} --command='
set -e

# Install runway-tools (requires a GitHub token with read on runway-tools).
# The token only needs to live on this VM, not in the laptop session.
read -sp "Paste a GitHub PAT with repo+read:org scopes: " GH_TOKEN; echo
pip install --user --quiet "git+https://x-access-token:${GH_TOKEN}@github.com/runway-lab/runway-tools.git"

# Generate the agent age key
mkdir -p ~/.config/age
age-keygen -o ~/.config/age/agent.key
chmod 600 ~/.config/age/agent.key
echo "Pubkey: $(grep '^# public key:' ~/.config/age/agent.key | cut -d: -f2- | xargs)"

# Clone secrets repo (the agent pulls this every cycle)
git clone https://x-access-token:${GH_TOKEN}@github.com/runway-lab/runway-secrets.git ~/.runway-secrets

# Set up rwy state dir
mkdir -p ~/.rwy
'
```

The pubkey printed in the last echo is what you PR next.

### 4.9. PR pubkey to runway-secrets

Back on the laptop (or from any machine with `gh`):

```bash
# Save the pubkey emitted by step 4.8 to a file
cd runway-secrets
cat > recipients/agents/gcp-${PROJECT_ID}.age.pub <<'EOF'
age1...your-pubkey...
EOF
git checkout -b agent/gcp-${PROJECT_ID}
git add recipients/agents/gcp-${PROJECT_ID}.age.pub
git commit -m "Register gcp-${PROJECT_ID} agent pubkey"
gh pr create --title "Register agent pubkey for gcp-${PROJECT_ID}" \
  --body "New GCP agent on project ${PROJECT_ID}. Admin merge."
```

Admin merges with `gh pr merge --admin --squash`.

### 4.10. Notify interns to re-register

Same as Slurm onboarding step 5 — interns re-run `rwy register wandb`
and `rwy register hf` so their per-user secrets are re-encrypted to
include the new GCP agent. Until they do, the agent will skip their
runs with `no_secrets_loaded`.

### 4.11. Start the agent

```bash
gcloud compute ssh rwy-agent-${PROJECT_ID} --zone=${ZONE} --command='
GITHUB_TOKEN="<the same PAT used in 4.8>" \
nohup ~/.local/bin/rwy-agent run \
  --backend gcp \
  --gcp-project '"${PROJECT_ID}"' \
  --gcp-region '"${REGION}"' \
  --gcp-artifacts-bucket gs://runway-'"${PROJECT_ID}"'-artifacts \
  --gcp-runtime-image '"${REGION}"'-docker.pkg.dev/'"${PROJECT_ID}"'/runway/runtime:v1 \
  --gcp-job-service-account rwy-job@'"${PROJECT_ID}"'.iam.gserviceaccount.com \
  --workspace ~/.rwy/work \
  --code-cache ~/.rwy/code-cache \
  --state-db ~/.rwy/state.db \
  --secrets-repo ~/.runway-secrets \
  --age-key ~/.config/age/agent.key \
  --agent-id gcp-'"${PROJECT_ID}"'-1 \
  --interval 60 \
  >> ~/.rwy/agent.log 2>&1 &

echo "PID=$!"
sleep 3 && tail -10 ~/.rwy/agent.log
'
```

The agent will refuse to start until `GcpBackend` is implemented in
runway-tools. Until then, this step is a dry run.

### 4.12. Smoke test

A spec with `backends: [gcp]` and a minimal `run:` block:

```yaml
apiVersion: runway/v1alpha1
kind: Experiment
metadata: {name: gcp-smoke, owner: <github-login>}
spec:
  code: {repo: runway-lab/<you>-experiments, ref: <sha>}
  resources: {gpus: 1, gpu_type: l4, max_hours: 0.25}
  backends: [gcp]
  selection: {policy: eta, profile_seconds: 0}
  run: |
    set -e
    python3 -c "import torch; print('gpu_avail=', torch.cuda.is_available()); print('gpu_count=', torch.cuda.device_count())"
    echo '{"step": 1, "total_steps": 1}'
    # Write something to AIP_MODEL_DIR so we see the artifact survive
    echo "hello from gcp" > "${AIP_MODEL_DIR:-/tmp}/marker.txt"
  artifacts:
    uri: "gs://runway-${PROJECT_ID}-artifacts/runs/{run_id}/candidates/{backend_id}/"
```

`rwy submit smoke.yaml` and verify:

1. PR auto-merged on `runway-jobs`.
2. Tracking issue opened with `backend:gcp` label.
3. Commit status `agent:gcp` flips through `pending → success`.
4. Agent log shows `submitted vertex ai job <name>`.
5. `gcloud ai custom-jobs list --region=${REGION}` shows the job.
6. `gs://runway-${PROJECT_ID}-artifacts/runs/<run_id>/candidates/gcp/marker.txt` exists.

If any of 1-5 fail, the agent should drain Vertex AI's error message
into the issue comment (mirrors the Slurm sbatch-stdout-drain pattern
from Vista quirk #4).

---

## 5. Predicted quirks (to be confirmed by first-run postmortem)

These are educated guesses based on Vertex AI docs, GCP quirks observed
elsewhere, and the patterns Vista revealed. Each may turn out fine, or
may need a patch like `clusters.py` got for Slurm. A postmortem doc
(`onboarding-gcp-<project>.md`) should record which actually bit.

### 5.1. Container image must be in same region (or pre-cached)

Cross-region image pulls add 1-3 minutes to job start. Build with
`--region=${REGION}` and only submit jobs in the same region.

### 5.2. `gpu_type: any` is rejected

Vertex requires concrete machine type + accelerator. The schema may
need to either reject `any` for `gcp` backend at PR-validate time, or
the `GcpBackend` raises before submit with a clear message.

### 5.3. CPU/RAM is fixed per machine type

`a2-highgpu-1g` is 1×A100 + 12 vCPU + 85 GB RAM. You don't request
CPU/RAM separately — the machine type pins it. If a spec needs more
RAM, you need `a2-highgpu-2g` (which gets you 2 GPUs whether you want
them or not).

### 5.4. Cloud Logging lag

Logs arrive 30-60s after `print()` in the container. The agent's
progress comments will trail the actual job state by that much. This
is acceptable but noticeably worse than Slurm's local `tail -F`.

### 5.5. Job state CANCELLING → CANCELLED is async

Unlike `scancel` (synchronous), `aiplatform.CustomJob.cancel()` returns
immediately and the job goes through `CANCELLING` for ~30s before
reaching `CANCELLED`. The poll loop must treat `CANCELLING` as
non-terminal but not re-claim it.

### 5.6. `displayName` is capped at 128 chars

`f"runway-{spec.metadata.name}-{run_id}"` may exceed when both are long;
truncate `spec.metadata.name` if needed.

### 5.7. GPU quota is project-wide and shared

Other workloads in the same project consume from the same quota pool.
The agent should `gcloud compute project-info describe` periodically and
log a warning when quota usage > 80%, but won't block.

### 5.8. AIP_* env vars require `baseOutputDirectory` to be set

If `artifacts.uri` placeholder expansion produces something that isn't
a `gs://` prefix, Vertex AI won't set `AIP_MODEL_DIR` and the training
script's `${AIP_MODEL_DIR}` references resolve to empty. Validate the
expanded URI starts with `gs://` at submit-time.

### 5.9. Cost in the billing console lags by hours

Don't expect to see a $0.50 smoke-test charge until the next day. Real-
time cost guards must use Vertex AI's `costEstimate` field on the job
itself (if available — TBD) or maintain a local accounting table from
known per-second machine-type rates.

### 5.10. `--scopes=cloud-platform` is broad

The VM's attached SA can do anything its IAM roles allow, gated by
scopes. `cloud-platform` is the union of all GCP APIs; relying on IAM
to be the actual fence. If your security model requires per-API
scoping, restrict scopes to `aiplatform`, `logging`, `devstorage`. Most
operators won't need to.

### 5.11. Workload identity federation orgs

Some enterprises forbid downloadable SA JSON keys entirely (everything
must go through workload identity federation). The VM-with-attached-SA
model still works — it uses instance metadata, not JSON keys — so this
restriction shouldn't block onboarding. But the human's laptop session
in steps 4.1-4.7 might need to use `gcloud auth login --impersonate-service-account`
instead of holding a JSON key locally.

---

## 6. Implementation gaps (what runway-tools needs)

This is the spec for the future `GcpBackend` PR. The Slurm PR sequence
was `runway-tools#1` (initial backend) → `#2` (cluster catalog) → `#3`
(parser fix) → `#4` (UTC timestamps) → `#5` (issue close). Expect
similar shape:

1. **`GcpBackend` class** (`rwy_agent.backend`) — `execute(spec, run_id, reporter)`:
   - Build CustomJob spec (machine type from gpu_type table, env vars,
     container image, scheduling.timeout, baseOutputDirectory).
   - Submit via `aiplatform.CustomJob.create()`.
   - Poll `CustomJob.state` every `--gcp-poll-interval` (default 30s).
   - Tail Cloud Logging entries since the last fetch; parse for progress
     lines / `<NAME>_URL=` lines like other backends.
   - On terminal state: success / failure mapped to exit codes.

2. **`providers.py`** (analog of `clusters.py`): `ProviderProfile`
   dataclass holding region defaults, gpu_type → machine_type mapping,
   any per-provider override. Initial entry: a generic GCP profile.

3. **`runway-tools/runtime/gcp/Dockerfile`** + `entrypoint.sh` for the
   runtime container.

4. **New CLI flags** on `rwy-agent run`: `--gcp-project`, `--gcp-region`,
   `--gcp-artifacts-bucket`, `--gcp-runtime-image`,
   `--gcp-job-service-account`, `--gcp-poll-interval`.

5. **Spec validator** in `runway-jobs/scripts/`: reject `gpu_type: any`
   when `backends: [gcp]`; reject unknown gpu_type values not in the
   provider table.

6. **Cost guardrail** (deferred but write it now while in flow): a
   per-agent flag `--gcp-max-cost-per-run-usd` that maps machine type
   × max_hours to predicted cost and refuses to submit if it exceeds
   the cap.

---

## 7. Codex on work-laptop — protocol

The operator is using Codex on a corporate laptop to drive this
onboarding. Specifics that differ from "human operator at terminal":

1. **`gcloud auth login` must be done by the human first** (step 3
   above). Browser OAuth + potential company SSO can't be driven by an
   automation agent. Once `gcloud config list` shows the right account
   active and a project set, hand off to Codex.

2. **Hand Codex the variables from section 2** explicitly. Don't rely
   on Codex guessing your PROJECT_ID or REGION. Suggested handoff
   prompt template at end of this section.

3. **Codex should not retain SA JSON keys**. The whole design uses
   `--service-account=...` on the VM (instance metadata) and `gcloud
   auth login` user creds for bootstrap — there's no SA JSON file to
   hand around. If Codex proposes downloading an SA key
   (`gcloud iam service-accounts keys create`), reject — the design
   doesn't need it.

4. **VPN / firewall**: corporate networks may block `git clone` from
   `github.com` over the VPN, or may require split tunneling. Confirm
   `git clone https://github.com/torvalds/linux.git --depth 1 /tmp/x`
   works from the laptop *before* handing off to Codex. If it fails,
   either configure the firewall or run step 4.8 onwards from the VM
   directly (where outbound HTTPS to GitHub is unrestricted by default
   on GCE).

5. **`gcloud compute ssh` from the laptop**: requires the laptop's SSH
   key to be propagated to the VM. `gcloud compute ssh` auto-creates
   one on first use. Codex driving this should be fine as long as the
   user runs the first `gcloud compute ssh ...` interactively to clear
   the "Updating instance metadata" + accept-key dialog, then
   subsequent invocations are non-interactive.

6. **Audit trail**: corporate environments often log all shell. A long
   Codex session running `gcloud iam`, `gcloud compute`, etc. is
   auditable. This is fine — these are standard ops commands; just be
   aware your IT can see what Codex did, including any secrets that
   accidentally land in argv.

### Handoff prompt template for Codex

```
Task: bootstrap a Runway GCP agent on this work laptop's gcloud session
following the doc at https://github.com/runway-lab/runway-jobs/blob/main/docs/onboarding-gcp.md

Variables (filled by me):
  PROJECT_ID = <your-project-id>
  REGION = <us-central1>
  ZONE = <us-central1-a>

I have already done step 3 (gcloud auth login + project set). Confirm
with `gcloud config list` before doing anything.

Execute sections 4.1 through 4.7 of the doc. After each step:
- Tell me what command you ran and the relevant output.
- Wait for my confirmation before proceeding to the next step.

Stop and report immediately if:
- Any gcloud command fails with PERMISSION_DENIED.
- GPU quota check (step 2) shows limit=0 for the gpu_type I need.
- You're about to write a JSON SA key file (the design forbids this).
- You hit a corporate firewall block.

For sections 4.8 onwards (SSH into VM): print the exact command you'd
run, but wait for me to execute it interactively first time (to clear
SSH-key dialog). After that you can drive `gcloud compute ssh ...
--command='...'` non-interactively.

Do NOT start the agent (step 4.11) — GcpBackend is not yet implemented
and the agent will refuse to start. Stop at step 4.10 (interns
re-register) and report.
```

---

## 8. Where to look next

- **Generic agent model** (read first): `docs/onboarding-agent.md`
- **Reference postmortem for the operator pattern**: `docs/onboarding-slurm-tacc-vista.md`
- **Spec validator + policies**: `policies/default.yaml`, `schemas/job.schema.json`
- **GCP region / quota status**: <https://console.cloud.google.com/iam-admin/quotas>
- **Vertex AI Custom Job ref**: <https://cloud.google.com/vertex-ai/docs/training/create-custom-job>
- **GCE service account auth via metadata**: <https://cloud.google.com/compute/docs/access/create-enable-service-accounts-for-instances>
