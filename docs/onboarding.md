# Onboarding (new intern)

One-time setup. About 10 minutes. You'll do steps marked **(you)**; an
admin handles steps marked **(admin)**.

Once finished, day-to-day usage is in `docs/routine.md`.

---

## 1. Accounts you need

**(you)** Make sure you have a GitHub account, a [wandb](https://wandb.ai)
account, and a [HuggingFace](https://huggingface.co) account. Use any
username — it doesn't have to match your GitHub login.

## 2. Get added to the org

**(admin)** Invite you to the `runway-lab` GitHub org as a *member*
(no repo collaborator access):

```bash
gh api -X PUT orgs/runway-lab/memberships/<your-github-login> -f role=member
```

**(you)** Accept the email invitation. Verify:

```bash
gh api orgs/runway-lab/memberships/<your-github-login> --jq .state
# → active
```

**(admin)** Also adds you to the HF `runway-lab` org as a **contributor**
and to the lab wandb team.

## 3. Generate your age keypair (local only)

`age` ([website](https://age-encryption.org)) is how we encrypt your
secrets. You hold the only private key.

**(you)** Install once:

```bash
# macOS
brew install age

# Linux without sudo
curl -sSL -o /tmp/age.tgz https://github.com/FiloSottile/age/releases/download/v1.2.1/age-v1.2.1-linux-amd64.tar.gz
tar xzf /tmp/age.tgz -C /tmp
mkdir -p ~/.local/bin && mv /tmp/age/age* ~/.local/bin/
```

Generate your keypair:

```bash
mkdir -p ~/.config/age && chmod 700 ~/.config/age
age-keygen -o ~/.config/age/runway.key
chmod 600 ~/.config/age/runway.key
grep "^# public key:" ~/.config/age/runway.key | sed 's/# public key: //'
```

The last line prints your **public** key (`age1...`). Copy it.

> ⚠️ **Never share** the contents of `~/.config/age/runway.key` itself.
> Only the public key (shown by the `grep` command) goes anywhere outside
> your machine.

## 4. Register your public key

**(you)** Open a PR to
[`runway-lab/runway-secrets`](https://github.com/runway-lab/runway-secrets)
adding a file `recipients/<your-github-login>.age.pub` containing your
public key on one line.

```bash
git clone git@github.com:runway-lab/runway-secrets.git
cd runway-secrets
echo "age1..." > recipients/<your-github-login>.age.pub
git checkout -b register-<your-github-login>
git add recipients/<your-github-login>.age.pub
git commit -m "Register <your-github-login>"
git push -u origin register-<your-github-login>
gh pr create --fill
```

**(admin)** Reviews and merges. `recipients/` is in CODEOWNERS so admin
review is required.

## 5. Generate wandb + HF tokens

**(you)** Two tokens:

### wandb API key
Go to <https://wandb.ai/authorize> and copy your key.
This is **one global key** for your account; wandb doesn't have
fine-grained scopes.

### HuggingFace fine-grained token
1. Open <https://huggingface.co/settings/tokens/new?tokenType=fineGrained>.
2. Name it `runway-agent-<your-github-login>`, set 90-day expiration.
3. Under **Repositories permissions → Organizations**: add `runway-lab`
   and tick **Read** and **Write** (only those two — not user namespace).
4. Save the `hf_...` string.

> The HF token can write to `runway-lab` org only. If it leaks, the
> blast radius is limited to that org; your personal HF namespace stays
> safe.

## 6. Upload your encrypted secret

For now, this is a one-step admin task; a `rwy register` CLI will
self-serve it later.

**(you)** Send your two tokens to the admin over a secure channel
(Signal, encrypted email — **not** plain Slack/email/issues).

**(admin)** Encrypts and PRs:

```bash
cd runway-secrets && git pull
printf 'WANDB_API_KEY=<wandb>\nHF_TOKEN=<hf>\nHF_ORG=runway-lab\n' \
  | age $(printf -- "-R %s " recipients/*.age.pub) \
        -o secrets/<your-github-login>.env.enc
git add secrets/<your-github-login>.env.enc
git commit -m "Add secret for <your-github-login>"
git push
```

After merge, the agent on the next poll cycle (≤ 1 minute) can decrypt
your secret when it sees a spec with `metadata.owner: <your-github-login>`.

## 7. Create your experiments repo

**(you)** Make a private GitHub repo under `runway-lab` to hold your
training code:

```bash
gh repo create runway-lab/<your-github-login>-experiments --private
git clone git@github.com:runway-lab/<your-github-login>-experiments.git
cd <your-github-login>-experiments
# add your train.py, requirements.txt, etc.
git push
```

## 8. Install the CLI

**(you)** Install `rwy`:

```bash
pip install "git+ssh://git@github.com/runway-lab/runway-tools.git#subdirectory=cli"
```

(`runway-tools` is private — you need to be in the `runway-lab` org for
the install to work.)

Sanity-check:

```bash
rwy --help
gh auth status   # confirm you're logged in as your own GitHub account
```

## 9. Run a smoke test

**(you)** Submit a tiny experiment using the example as a template.
Copy `examples/job.yaml` from this repo and edit `metadata.owner` to
your GitHub login and `spec.code.repo` to your experiments repo:

```bash
rwy submit my-smoke.yaml
```

The CLI prints a PR URL and a `run_id`. Watch:

- The PR auto-merges (≤ 1 minute) once validation passes.
- A tracking issue opens on this repo with title `[run] ... — <run_id>`.
- The agent claims your spec, decrypts your secrets, clones your code,
  runs it.
- Your wandb dashboard shows the run live (matching `<run_id>`).
- Your HF org shows a new repo `runway-lab/runs-<run_id>` after the
  script finishes.

Use `rwy status <run_id>` to check from the command line. You're done.

---

## What was just set up, in one picture

```
   you                 GitHub                      4vita (agent)
   ┌──────┐            ┌──────────────┐            ┌─────────────────┐
   │ rwy  │── PR ────▶│  runway-jobs  │── poll ──▶│  rwy-agent       │
   │ CLI  │            │  (public)     │            │   ├ clone code   │
   └──┬───┘            │  ├ jobs/      │            │   ├ age-decrypt  │
      │                │  └ auto-merge│            │   │   secrets    │
      │ git push       └──────┬───────┘            │   ├ pip install  │
      ▼                       │ (admin: CODEOWNERS)│   └ python ...   │
   ┌──────────────┐            │                    └────┬────────────┘
   │ your-experi- │            ▼                          │
   │ ments (priv) │   ┌──────────────┐                    │ env: WANDB_API_KEY, HF_TOKEN
   └──────────────┘   │ runway-secrets│ ◀── decrypt ───── (only your.env.enc)
                      │   (private)   │
                      └──────────────┘
                                                          ▼
                                              wandb.ai/<you>/...   ─── live metrics
                                              huggingface.co/runway-lab/runs-...
```

## Where to look next

- **Day-to-day workflow**: `docs/routine.md`
- **What can / can't go in a spec**: `policies/default.yaml` (everything
  in there is validated on every PR)
- **GitHub-side setup, for admins**: `docs/github-setup.md`
- **Architectural decisions**: `docs/discussions.md`
