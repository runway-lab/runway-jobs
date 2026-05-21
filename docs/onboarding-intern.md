# Intern onboarding (new researcher)

One-time setup for someone who wants to **submit jobs** via `rwy`. Takes
~5 minutes once you're in the org. Most of it is self-service via
`rwy register`.

If you're instead bringing a new compute environment (Slurm cluster,
SSH host, GCP project) into runway, see `docs/onboarding-agent.md`.

Day-to-day usage is in `docs/routine.md`.

---

## 1. Accounts you need

**(you)** Make sure you have:
- A GitHub account (you'll need to be added to the `runway-lab` GitHub org).
- A [wandb](https://wandb.ai) account.
- A [HuggingFace](https://huggingface.co) account.

The wandb / HF usernames don't have to match your GitHub login.

## 2. Get added to the org

**(admin)** invites you:

```bash
gh api -X PUT orgs/runway-lab/memberships/<your-github-login> -f role=member
```

Plus:
- HF: add you to the `runway-lab` HF org as **Contributor**.
- wandb: add you to the lab wandb team as **Member**.

**(you)** Accept the GitHub email invitation.

## 3. Install age

`age` is how we encrypt your tokens. Install once:

```bash
# macOS
brew install age

# Linux without sudo
curl -sSL -o /tmp/age.tgz https://github.com/FiloSottile/age/releases/download/v1.2.1/age-v1.2.1-linux-amd64.tar.gz
tar xzf /tmp/age.tgz -C /tmp
mkdir -p ~/.local/bin && mv /tmp/age/age* ~/.local/bin/
```

(`age-keygen` and `age` should be on `$PATH`.)

## 4. Install the CLI

```bash
pip install "git+ssh://git@github.com/runway-lab/runway-tools.git#subdirectory=cli"
```

This requires being in the `runway-lab` org (the repo is private).

## 5. Generate your keypair + register pubkey

```bash
rwy register keygen
```

This:
- Generates `~/.config/age/runway.key` (mode 600 — **keep secret**).
- Prints your public key.
- Opens a PR to `runway-lab/runway-secrets` adding
  `recipients/<your-github-login>.age.pub`.
- The repo's auto-merge gate sees the path matches your login → merges
  within a minute or two.

> ⚠️ **Never share** `~/.config/age/runway.key`. Only the public key
> (already in the PR) goes outside your machine. If you lose this file,
> any secret encrypted to you becomes unrecoverable — re-run `rwy
> register keygen --force` and re-upload your tokens.

## 6. Upload your wandb key

Get your wandb API key from <https://wandb.ai/authorize> then:

```bash
rwy register wandb
# (prompts for API key, hidden input)
```

This:
- Clones runway-secrets to a temp dir.
- Decrypts your existing `secrets/<you>.env.enc` (if any) with your age
  private key.
- Adds (or updates) `WANDB_API_KEY`.
- Re-encrypts the merged secret to every recipient in
  `recipients/*.age.pub` (including all agent hosts).
- PRs the new ciphertext. Auto-merges within a minute or two.

You can pass `--entity <wandb-team>` if you want runs to default to a
specific wandb team.

## 7. Upload your HF token

1. Create a fine-grained token at <https://huggingface.co/settings/tokens/new?tokenType=fineGrained>
2. Name it `runway-agent-<your-github-login>`.
3. **Repositories permissions → Organizations → `runway-lab`**: check
   **Read** and **Write** (only those — no user-namespace access).
4. Save the `hf_...` token.

Then:

```bash
rwy register hf
# (prompts for token, hidden input)
```

Same flow as wandb: decrypt → upsert HF_TOKEN + HF_ORG → re-encrypt → PR.

## 8. Create your experiments repo

```bash
gh repo create runway-lab/<your-github-login>-experiments --private --clone
cd <your-github-login>-experiments
# add train.py, requirements.txt, ...
git push
```

The CLI expects a `train.py` (or any script you point `spec.run` at) in
this repo. See `runway-lab/jurray-experiments` for a minimal example.

## 9. Smoke test

```bash
# Get the latest commit SHA of your experiments repo
SHA=$(git -C path/to/<you>-experiments rev-parse HEAD)

# Write a spec — or copy examples/job.yaml from runway-jobs and edit
cat > /tmp/smoke.yaml <<EOF
apiVersion: runway/v1alpha1
kind: Experiment
metadata: {name: hello, owner: <your-github-login>}
spec:
  code: {repo: runway-lab/<your-github-login>-experiments, ref: $SHA}
  resources: {gpus: 0, gpu_type: any, max_hours: 1}
  backends: [ssh]
  selection: {policy: eta, profile_seconds: 0}
  run: |
    pip install --user -q -r requirements.txt
    python train.py
  artifacts: {uri: "gs://\${ARTIFACTS_BUCKET}/runs/{run_id}/candidates/{backend_id}/"}
EOF

rwy submit /tmp/smoke.yaml
```

You should see:
- A new PR on `runway-lab/runway-jobs` auto-merging within a minute.
- A new GitHub issue `[run] hello — <run_id>` tracking your run.
- A wandb run appearing in your dashboard with your `run_id`.
- A new HF repo `runway-lab/runs-<run_id>` after the script finishes.

`rwy status <run_id>` gives a CLI summary.

---

## In one picture

```
   you (local)             GitHub (public)            agent host
   ┌──────────┐            ┌─────────────────┐       ┌──────────────────┐
   │ rwy      │── PR ────▶│  runway-jobs     │       │  rwy-agent       │
   │ submit   │            │  ├ jobs/ (spec) │── poll ▶ ├ git pull       │
   └────┬─────┘            │  └ auto-merge   │       │ ├ age decrypt   │
        │                  └─────────────────┘       │ │   own secret  │
        │ git push                                    │ ├ git clone code│
        ▼                  ┌─────────────────┐       │ └ python ...    │
   ┌──────────┐            │  runway-secrets │       │                  │
   │ <you>-   │            │  ├ recipients/  │◀── pull (every cycle)
   │ experi-  │            │  └ secrets/.env.enc
   │ ments    │            │     (age, multi-recipient)
   └──────────┘            └─────────────────┘
                                  ▲
                                  │ PRs from `rwy register` auto-merge
                                  │ only if every changed file is
                                  │ secrets/<you>.env.enc or
                                  │ recipients/<you>.age.pub
```

## What's protecting what (the short version)

| Concern | What stops it |
|---|---|
| Bob reading Alice's wandb/HF tokens | age multi-recipient — Bob's private key is not in the recipient list of `secrets/alice.env.enc` |
| Bob uploading a fake secret for Alice | runway-secrets gate workflow — PR touching `secrets/alice.env.enc` from author=bob is not auto-merged; admin review required |
| Bob impersonating Alice in a spec submission | runway-jobs validator — rejects PRs where `metadata.owner ≠ PR author` |
| Bob modifying or deleting Alice's existing spec | runway-jobs `check_pr_ownership.py` — rejects modify/delete of files originally authored by someone else |
| Bob writing to Alice's HF repos | HF org Contributor role — limits write to repos you created |
| Bob editing Alice's wandb runs | wandb team Member role — limits edit/delete to your own runs |

## Looking up your stuff

- Wandb dashboard: `https://wandb.ai/<your-wandb-entity>/runway-smoke`
- HF: `https://huggingface.co/runway-lab/runs-<run_id>`
- Recent submissions: `rwy list`
- One run: `rwy status <run_id>`

## Where to look next

- **Day-to-day workflow**: `docs/routine.md`
- **What can / can't go in a spec**: `policies/default.yaml`
- **Bringing a new compute env (Slurm/SSH/GCP) in**: `docs/onboarding-agent.md`
- **GitHub-side setup, for admins**: `docs/github-setup.md`
- **Architectural decisions**: `docs/discussions.md`
