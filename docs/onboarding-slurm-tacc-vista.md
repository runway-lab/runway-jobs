# Slurm agent onboarding — TACC Vista postmortem

**Lab:** runway-lab.
**Cluster:** TACC Vista (Grace-Hopper, aarch64).
**agent-id:** `slurm-tacc-vista-1`.
**Operator:** `zhuconv` (admin on runway-lab/{runway-jobs, runway-tools, runway-secrets, zhuconv-experiments}).
**First green smoke:** run `20260523-003123-1e4268b1` (Slurm job 718911, partition `gh`, COMPLETED 8s on `c621-061`).
**End-to-end working with full lifecycle (open → close + label swap):** run `20260523-062219-5143cbc6` (Slurm job 719156, partition `gh`, COMPLETED 8s on `c621-121`, issue #53 CLOSED with `status:succeeded` 2m2s after submit).

The main doc (`runway-lab/runway-jobs/docs/onboarding-agent.md`) is correct as-is for "generic Slurm." This note records site-specific gotchas TACC Vista exposed and the code changes needed to land them. Use as a checklist + heads-up for the next Slurm cluster you onboard.

---

## Variables resolved (Vista-specific)

| | Value | Where from |
|---|---|---|
| Cluster name (for `--cluster` flag and agent-id prefix) | `tacc-vista` | site name |
| Slurm partition | `gh` | production GH200 partition; `gh-dev` has shorter MaxTime + reservation churn, not worth it for the agent |
| Account | `ASC26009` | `SLURM_TACC_ACCOUNT` env; user's only billable allocation |
| Workspace root | `/scratch/11012/$USER/rwy/{work,code-cache}` | TACC stockyard layout — **`$SCRATCH`, never `/scratch/$USER`** |
| Agent install footprint | `~/.local/bin/{age,rwy,rwy-agent}` + `~/.local/share/runway-venv/` (24 MB) | NFS — visible from compute + login |
| age private key | `~/.config/age/agent.key` (600) | NFS, but stays on host |
| Secrets clone | `~/.runway-secrets` | NFS |

`$SCRATCH` is 13 PB unbacked, `$WORK` is 1 TB Lustre, `$HOME` (`/home1`) is 24 GB hard quota. Per-run workdirs go on `$SCRATCH` only — `$HOME` fills in 20-100 runs, then the agent crashes on every write.

---

## TACC quirks (vs. generic Slurm assumed by the doc)

These are the cluster-side behaviors that broke the agent before patches landed. They are likely to recur on other large-site Slurm deployments (TACC Frontera, TACC Stampede, NCSA Delta, OLCF, etc.).

### 1. No `Gres` on any partition

`scontrol show node` returns `Gres=(null)` on every Vista node, `sinfo -o '%P %G'` shows `(null)` for `gh`, `gh-dev`, and `gg`. GPUs are attached implicitly via partition routing — the `gh` partition is homogeneous GH200, so requesting the partition gets you the GPU.

`#SBATCH --gres=gpu:1` or `--gres=gpu:any:1` is **rejected by slurmctld** because no such resource is defined. The doc says "GPU resource string must match `scontrol show node` Gres output" — on Vista that match is "don't emit `--gres` at all."

→ Patched via `rwy_agent.clusters` catalog with `gres_mode="none"` for `tacc-vista`.

### 2. `gh` partition requires explicit `-N` / `--nodes`

`sbatch` without `--nodes=N` is rejected:

```
--> Submission error: please define total node count with the "-N" option
```

This message is printed on **stdout, not stderr** (see quirk 4). The spec doesn't carry a node count — the agent didn't emit `--nodes` by default before the catalog work.

→ Patched via `extra_sbatch_lines=("#SBATCH --nodes=1",)` in the `tacc-vista` profile.

### 3. `sbatch --parsable` is preceded by a Vista welcome banner

What `sbatch --parsable script.sh` actually returns on Vista (stdout, in order):

```
-----------------------------------------------------------------
          Welcome to the Vista Supercomputer
-----------------------------------------------------------------

No reservation for this job
--> Verifying valid submit host (login1)...OK
--> Verifying valid jobname...OK
--> Verifying valid ssh keys...OK
--> Verifying access to desired queue (gh)...OK
--> Checking available allocation (ASC26009)...OK
--> Quotas are not currently enabled for filesystem /home1/...OK
718911
```

The old agent parser was `r.stdout.strip().split(";", 1)[0]` (handles federation's `<jobid>;<cluster>`), which kept the entire banner as `job_id` and then crashed constructing `workdir / f"slurm-{job_id}.out"` (path > 255 chars → OSError Errno 36). The Slurm job ran fine; the agent just lost track of it (matches doc caveat #1 "no orphan recovery", but as a parser bug instead of a restart).

→ Patched: parse last non-blank line of stdout, drop `;<cluster>` suffix, `isdigit()` guard before using as path component. Anything that fails the guard surfaces as a clean `reporter.failed` with the raw stdout in the tail, not a path-construction crash.

### 4. sbatch errors land on stdout, not stderr

`subprocess.CalledProcessError.stderr` is empty when sbatch refuses a job; the actual message is in `.stdout`. The old `tail = (e.stderr or "").splitlines() or ["sbatch rc=N"]` always degenerated to the placeholder, hiding the real reason.

→ Patched: drain both `e.stderr` and `e.stdout` into the failure tail (`runway-tools` PR #2). Combined with the parser fix in PR #3, sbatch failures now produce useful messages in the tracking-issue comment.

### 5. Compute nodes cannot submit, even with the binary present

`/usr/bin/sbatch` is on `$PATH` on compute nodes (e.g. inside an `idev` allocation), but slurmctld rejects with `[TACC]: Job submission is not allowed from this host. Please submit through one of the available login resources.` Additionally, an interactive-shell shim shadows `sbatch` with a `bash function` printing the same message — bypass with `/usr/bin/sbatch` calls the real binary but slurmctld still says no.

Implication: rwy-agent **must run on a login node**, period. Wrapping the agent in a batch job to "make it persistent" is not an option on TACC.

### 6. Login nodes are MFA-only — non-interactive SSH impossible

TACC enforces password + 6-digit token MFA on `login1`. Pubkey auth is disabled. The agent cannot be started from a script that ssh's into login1 — an operator has to type their MFA token live, in their own interactive ssh session, to launch `nohup rwy-agent ...`.

In practice this means: every restart of the agent (every code change, every cluster downtime) requires a human at a terminal. There is no headless way to drive this.

### 7. Login-node "automation policy" is fuzzy

[TACC's Vista docs](https://docs.tacc.utexas.edu/hpc/vista/) say only:

> Vista's login nodes are a shared resource.
> ... ensure that cronjobs are run only on the compute nodes.

It does **not** explicitly forbid long-running daemons on login nodes. A 60s-poll, ~MB-memory agent is well within "fair share." Behavior observed during this onboarding: nothing flagged, no ops contact. But the agent is one site policy change away from being killed — keep `--interval` high (default 60s is fine; bump to 300s in dev-quiet windows) and don't add CPU-heavy local work.

### 8. `$SCRATCH` 10-day purge is not actually a problem for an active agent

TACC says scratch is purged after 10 days of inactivity. With an active agent writing new run workdirs every spec, the scratch tree's mtime is fresh — no purge fires. If the agent goes offline for 10+ days, old workdirs may be reaped (state.db on `$HOME` is unaffected, so the agent recovers fine; it just can't introspect prior run outputs).

### 9. `sacct` reports local time, not UTC

Unlike everything else we normalized to UTC (run_ids, agent.log lines, GitHub timestamps), `sacct -j NNNN --format=Submit,Start,End` columns are in the cluster's configured local TZ (US/Central). The agent doesn't read these for decisions, so it's cosmetic, but anyone correlating sacct output with run_id / agent.log timestamps must mentally add 5 hours (UTC vs CDT). No fix planned — that's TACC's slurmd config.

---

## Code shipped during this onboarding

| Repo | PR | What |
|---|---|---|
| `runway-secrets` | #7 | `recipients/agents/slurm-tacc-vista.age.pub` (added then auto-merged by admin) |
| `runway-tools` | [#2](https://github.com/runway-lab/runway-tools/pull/2) | `rwy_agent.clusters` catalog (first entry `tacc-vista` with `gres_mode="none"` + `extra_sbatch_lines=("#SBATCH --nodes=1",)`); `--cluster` CLI flag; sbatch failure tail drains both stderr and stdout; `Resources.gpus >= 1` at the spec parser; bumped to 0.2.0. |
| `runway-tools` | [#3](https://github.com/runway-lab/runway-tools/pull/3) | Parse `--parsable` jobid as the last non-blank line of stdout + `isdigit()` guard. Handles TACC's welcome-banner prefix. |
| `runway-tools` | [#4](https://github.com/runway-lab/runway-tools/pull/4) | Agent logs in UTC ISO 8601 with `Z` suffix (`logging.Formatter.converter = time.gmtime`); `rwy submit` tags output with `(UTC)`. Run_id and log timestamps now match. |
| `runway-tools` | [#5](https://github.com/runway-lab/runway-tools/pull/5) | `reporter.succeeded` / `reporter.failed` close the tracking issue (`state_reason="completed"` / `"not_planned"`) and swap `status:running` → `status:succeeded` / `status:failed`. Preserves non-`status:*` labels. One atomic PATCH. |
| `runway-jobs` | [#43](https://github.com/runway-lab/runway-jobs/pull/43) | `schemas/job.schema.json`: `gpus.minimum` 0 → 1 (reject `gpus: 0` at validate-CI rather than letting sbatch reject it with a confusing message). |
| `runway-jobs` | [#46](https://github.com/runway-lab/runway-jobs/pull/46) | `docs/onboarding-agent.md`: UTC convention note at the top. |
| `runway-jobs` | [#49](https://github.com/runway-lab/runway-jobs/pull/49) | Workflow puts `@owner` in issue title; `gh label create --force` covers `status:succeeded` and `status:failed` so the agent's terminal PATCH doesn't 404 on a fresh repo. |

Side artifact: `runway-lab/zhuconv-experiments` (private) — created with a default README to satisfy `spec.code.repo` `git clone` for smoke specs. The smoke `run:` block is inline shell, so repo content is irrelevant; it just needs to exist.

---

## How the agent is launched on login1

Operator-facing, run once per restart in an interactive ssh session:

```bash
pkill -f 'rwy-agent.*slurm-tacc-vista-1'  # kill old instance
sleep 2 && pgrep -af rwy-agent              # confirm dead

mkdir -p ~/.rwy
GITHUB_TOKEN="$(env -u GITHUB_TOKEN gh auth token)" \
nohup ~/.local/bin/rwy-agent \
  --backend slurm --cluster tacc-vista \
  --workspace /scratch/11012/$USER/rwy/work \
  --code-cache /scratch/11012/$USER/rwy/code-cache \
  --state-db ~/.rwy/state.db \
  --secrets-repo ~/.runway-secrets \
  --age-key ~/.config/age/agent.key \
  --agent-id slurm-tacc-vista-1 \
  --slurm-partition gh --slurm-account ASC26009 \
  --slurm-poll-interval 30 --interval 60 \
  >> ~/.rwy/agent.log 2>&1 &
echo "PID=$!"
sleep 3 && tail -5 ~/.rwy/agent.log
```

Production tuning: bump `--slurm-poll-interval` and `--interval` to `3600` once development stabilises (was the operator's call — saves API budget at the cost of slower spec pickup).

---

## Onboarding a different Slurm cluster — checklist

For the next site (say `mit-csail`), do this in order:

1. **Variables to nail down before touching anything**
   - `cluster-name` (becomes `--cluster` value and `agent-id` prefix)
   - `partition`, `account` — confirm submit allowed with `sacctmgr show user $USER`
   - shared FS path visible to compute (avoid `~` if home isn't mounted on compute)
   - GitHub PAT or `gh` auth on the agent host with `repo` + `read:org`
   - GPU detection: `scontrol show node <one node> | grep Gres` — decide `explicit_type` vs `any_type` vs `none`

2. **Add a `ClusterProfile` to `runway-tools/src/rwy_agent/clusters.py`** matching what `sbatch` actually wants. Vista's entry is the reference. If you discover the partition rejects `--nodes`-less jobs, copy the `extra_sbatch_lines` pattern. PR + admin-merge.

3. **On the agent host (must be login/head node — see quirk 5)**, run the install bundle from the doc's "common steps":
   - `pip install --user "git+https://x-access-token:${GH_TOKEN}@github.com/runway-lab/runway-tools.git"`
   - `age-keygen -o ~/.config/age/agent.key && chmod 600 ~/.config/age/agent.key`
   - Note the pubkey from `grep '^# public key:' ~/.config/age/agent.key`
   - `git clone https://github.com/runway-lab/runway-secrets.git ~/.runway-secrets`
   - `mkdir -p <shared-fs>/rwy/{work,code-cache} ~/.rwy`

4. **PR the pubkey to `runway-lab/runway-secrets`** under `recipients/agents/slurm-<cluster-name>.age.pub`. CODEOWNERS forces admin review (intentional — agents are trust-elevated; see `runway-secrets/.github/workflows/auto-merge-own-files.yml`). Admin `--admin` merge.

5. **Tell existing interns to re-run `rwy register wandb` and `rwy register hf`** so their per-user secrets are re-encrypted to include the new agent. Per-user secret files in `secrets/<login>.env.enc` won't decrypt for the new agent until they do. The agent will skip such runs with a `no_secrets_loaded` log line — not a hard failure, but those interns' runs can't access wandb/HF.

6. **Run a smoke spec** with `gpus: 1`, `backends: [slurm]`, inline echo for `run:`. Same shape as `slurm-smoke-v5.yaml`. `rwy submit ./smoke.yaml`. Verify the 6-item checklist from the doc, especially that the issue closes on terminal state.

7. **Watch the first real spec end-to-end**. If sbatch fails, the new error path drains stdout into the issue comment — read it instead of guessing. If the agent crashes, `tail -100 ~/.rwy/agent.log` has the traceback (and now it's UTC-timestamped, so it lines up with the run_id).

---

## Known not-fixed (low-priority follow-ups)

- **Orphan-job recovery on restart** (doc caveat #1) — still no `state.db` resume of in-flight Slurm jobs. If the agent is killed mid-run, the Slurm job keeps running but the agent forgets. Mitigation today: drain before restart, or `scancel` known orphans first.
- **`sacct` latency**: agent treats absence-of-row as `PENDING` and retries. Fine on Vista; bump `--slurm-poll-interval` if a busier site shows wrong "still pending" comments.
- **No CPU-only spec shape**: spec now requires `gpus >= 1`. CPU-only experiments should use the `ssh` backend until a dedicated resource shape lands.
- **No `failed` re-run flow**: a failed run requires changing `spec.code.ref` (forces a new run_id). Re-submitting the same spec under the same `metadata.name` works (filename keys on run_id), but doesn't act as a "retry."
- **TACC sacct local time** (quirk 9): cosmetic, not fixed.
- **Issue title transitions**: the title doesn't reflect terminal state (e.g. `[done] ...` or `[fail] ...`). Issue close + label is the canonical signal; title prefix change would be cosmetic.
