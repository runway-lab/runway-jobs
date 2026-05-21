# Routine (existing intern)

Day-to-day flow for submitting an experiment. Assumes onboarding
(`docs/onboarding.md`) is done.

About 30 seconds per submission, zero admin involvement.

---

## The loop

```
edit code → push → write spec → rwy submit → watch wandb
```

### 1. Iterate on your training code

```bash
cd ~/<your-github-login>-experiments
# edit train.py, change hyperparams, whatever
git add . && git commit -m "lr=5e-4, dropout=0.1"
git push
SHA=$(git rev-parse HEAD)
```

Pin a **commit SHA** in the spec, not `main`. That makes the run
reproducible — even if you push more commits later, this run is locked
to this SHA.

### 2. Write (or copy) a spec

A spec is YAML. Copy your last one and tweak, or start from
`examples/job.yaml`. Required fields:

```yaml
apiVersion: runway/v1alpha1
kind: Experiment
metadata:
  name: <short-name-for-this-run>   # any short string
  owner: <your-github-login>         # MUST match your GitHub login
spec:
  code:
    repo: runway-lab/<your>-experiments
    ref: <commit-sha>                 # NOT main, use the SHA from step 1
  resources:
    gpus: 1
    gpu_type: any                     # or A100, H100, ...
    max_hours: 4
  backends:
    - ssh                             # ssh, gcp, slurm — pick the ones you want
  selection:
    policy: eta
    profile_seconds: 60
  run: |
    # The script that runs inside your code repo (cwd = your repo root)
    pip install --user -q -r requirements.txt
    python train.py --lr 5e-4
  artifacts:
    uri: gs://${ARTIFACTS_BUCKET}/runs/{run_id}/candidates/{backend_id}/
```

> **Owner check is enforced**: if `metadata.owner` doesn't match the
> GitHub user opening the PR, CI rejects the PR. You can't submit a
> spec impersonating someone else.

### 3. Submit

```bash
rwy submit my-spec.yaml
```

The CLI:
- Generates a `run_id` (timestamp + random hex).
- Forks `runway-lab/runway-jobs` to your account if you haven't already.
- Creates a branch `submit/<run_id>` in your fork.
- Commits `jobs/<run_id>.yaml` and pushes.
- Opens a PR back to the registry.
- Prints the PR URL + `run_id`.

```
submitted: 20260521-181055-b5169aae
  PR:     https://github.com/runway-lab/runway-jobs/pull/28
  branch: submit/20260521-181055-b5169aae
  next:   the PR will auto-merge once `validate` passes; watch with
          `rwy status 20260521-181055-b5169aae`.
```

### 4. Watch

Three places show you what's happening:

- **GitHub issue** auto-opened titled `[run] <name> — <run_id>`. The
  agent posts a live comment that gets edited every ~30 s with the
  current phase + progress bar + wandb URL + HF URL.
- **wandb dashboard** at `wandb.ai/<your-username>/runway-smoke/runs/<run_id>`
  with whatever metrics your `train.py` logs.
- **`rwy status <run_id>`** from the command line for a quick check.

### 5. Done

When the run finishes:

- The commit status becomes `agent:ssh = success`.
- The wandb run is marked complete with your summary metrics.
- Artifacts (whatever your `train.py` writes to `artifacts/`) are pushed
  to `huggingface.co/runway-lab/runs-<run_id>` as a private repo.
- The GitHub issue stays open as a record; the agent's last comment
  contains both URLs.

---

## What the agent actually does for you

When `rwy submit` PR merges, somewhere a `rwy-agent` process is polling
`main` every ~60 s. When it sees your spec:

1. Decides if it can serve your `backends` (each agent serves one).
2. Claims the spec (other agents for the same backend will skip it).
3. `git pull`s the `runway-secrets` repo, **decrypts** `secrets/<your-github-login>.env.enc` using its own age private key — gets your `WANDB_API_KEY`, `HF_TOKEN`, etc.
4. `git clone --fetch --checkout` your code repo at the SHA you pinned.
5. Runs your `spec.run` as a subprocess with cwd set to your code root,
   env containing your decrypted secrets plus `RWY_RUN_ID`,
   `WANDB_RUN_ID`, `WANDB_PROJECT`.
6. Reads your stdout for `{"step": N, "total_steps": M, ...}` JSON lines
   to drive the progress bar, and for `WANDB_URL=...` / `HF_REPO_URL=...`
   lines to link in the issue.
7. Posts a final `success` or `failure` commit status.

Your secrets are **never** in the spec, never in any PR, never in any
issue comment, never on disk in plaintext outside the subprocess
runtime.

## Spec rules you'll bump into

The validator (running on every PR) checks against `policies/default.yaml`:

- `resources.gpus ≤ 4`, `max_hours ≤ 8`, `selection.profile_seconds ≤ 900`
- `backends` ⊂ `[gcp, slurm, ssh]`
- `code.repo` must start with `runway-lab/`
- `metadata.owner` must equal **your GitHub login**
- `artifacts.uri` must start with `gs://${ARTIFACTS_BUCKET}/` or
  `s3://${ARTIFACTS_BUCKET}/`
- No raw credentials, internal hostnames, internal IPs (10.x, 192.168.x),
  `.corp` / `.internal` / `.intra` domains, etc. in any string field.

If validation fails, the PR shows the error in the `schema + policy`
check. Fix and push another commit — the same PR re-validates.

CI also enforces **per-file ownership** on modifications and deletions:
you can only modify, delete, or rename `jobs/<run_id>.yaml` files
*you* originally created. Trying to touch someone else's spec — even
to rewrite its `owner` to your own login — gets rejected. New specs
(adds) are governed by the `metadata.owner == PR author` rule above.

## Things to keep in mind

- **Pin commit SHAs, not `main`**. Otherwise reproducing a result later
  needs guesswork about which commit you actually ran.
- **One spec per run**. Don't try to bundle multiple experiments in one
  YAML — split them.
- **Don't commit secrets** to your `<you>-experiments` repo. The
  runway-secrets flow exists exactly so you never need to.
- **`max_hours` is a contract, not a wall**. The validator enforces the
  policy ceiling but the agent doesn't yet kill long-running processes
  (Phase 3 work). Set it conservatively.
- **Artifacts beyond a few MB belong on wandb / HF**, not stuffed into
  stdout or commit comments. Use `wandb.log_artifact()` for medium files
  and HF push for final checkpoints.

## Looking at past runs

```bash
rwy list                 # your recent PRs / runs
gh issue list --repo runway-lab/runway-jobs --label run --search "owner:<you>"
```

For wandb, just go to your project page; for HF, your runs each get a
repo named `runway-lab/runs-<run_id>`.

## When something goes wrong

| Symptom | Likely cause | What to do |
|---|---|---|
| Validate check fails on PR | spec breaks a policy rule | Read the error, fix the spec, push again |
| PR opened but never auto-merges | spec touches non-`jobs/` paths | Make sure your PR only adds one file under `jobs/` |
| Agent claims but commit status stays `pending` for >5 min | training got stuck before printing any progress JSON | SSH the agent host (or ask admin) — look at `~/runway/work/<run_id>/stdout.log` |
| `failed` with `train.py: No such file or directory` | your `spec.code.repo` / `code.ref` is wrong | Double-check the repo name and SHA |
| `failed` and stdout says `WANDB_API_KEY` is unset | onboarding step 6 (encrypted secret upload) wasn't done | Ping admin |
| wandb shows the run under a different username than expected | `WANDB_API_KEY` belongs to a different account | Re-do step 5 of onboarding with the right account's key |

If you're not sure what happened, paste the run_id in the lab channel
and an admin can check the agent host directly.
