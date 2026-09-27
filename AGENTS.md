# AGENTS.md — OWUI-tools

Mono-repo of containerized tools around Open WebUI, composed via Docker
Compose include-trees. Python-first. Deploy target: .209.

## Layout

- Each subdir is a self-contained service (own Dockerfile, compose, README).
- `deprecated/` holds unmaintained variants — do not extend them.
- Root `docker-compose.yml` — tracked include-composition of all subdir
  services (its header comment claiming git-exclusion is stale).
- `local.docker-compose.yml` and `local2.docker-compose.yml` — git-excluded
  smoke overrides (`local.*` gitignore rule / `.git/info/exclude`). These
  files DEFINE the `ollama-tools` and `searxng` networks.

## Hard rules

- **Branch `dev` only — never commit to `main`.** Deployment pulls `dev`.
- **Docker-only tooling.** The host stays clean: no pip installs, no host
  venvs. Host `python3` stdlib scripts (no third-party imports) are allowed.
- **Run compose from the repo ROOT.** Subdir compose files reference the
  `ollama-tools` network without defining it, so `docker compose` inside a
  subdir fails on the undefined network. Use
  `docker compose -f local.docker-compose.yml up -d --build <service>`.
- Local commits only; **no push without explicit user instruction**.
- Never commit secrets — `.env` is gitignored everywhere; every service
  ships `.env.example`.

## Conventions

- Conventional commits: `feat|fix|chore|docs|test(scope): summary`.
- Subdir services join the shared `ollama-tools` network.
- Production on .209 uses `/home/shadow01/docker-compose.yml` (open-webui
  stack), which includes the same subdir compose files.
- websearch-mcp serves reddit.com URLs through `reddit_backend.py`
  (anonymous .rss Atom feeds, rate-gated + cached); never route reddit
  through the browser/stealth escalation tiers — they cannot read comments.
