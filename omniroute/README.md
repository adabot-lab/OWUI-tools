# OmniRoute (testing, alongside litellm)

OmniRoute is an LLM API router with a built-in dashboard. This is a testing
deployment set up alongside the existing litellm proxy — same repo, additive
only; litellm is untouched and remains the production router until promotion.

## Port layout (two-port variant)

| Port | Purpose | Published via |
|------|---------|---------------|
| 20128 | Dashboard | `local.docker-compose.yml` (per-host, gitignored) |
| 20129 | `/v1` API | `local.docker-compose.yml` (per-host, gitignored) |
| 20132 | Live WebSocket | internal only (loopback-bound by default) |

The tracked `docker-compose.yml` leaves ports unpublished (litellm pattern);
each host publishes 20128/20129 in its own `local.docker-compose.yml` override.
Redis runs as the `omniroute-redis` sidecar (`REDIS_URL=redis://omniroute-redis:6379`) — renamed from upstream's `redis` to avoid a service-name conflict with the searxng stack in the shared include tree.

## Credentials: dashboard-only for ALL providers

No provider API keys in `.env` (user decision 2026-08-21). OmniRoute v3.8.0
dropped env-var credentials for openrouter/glm/mistral/groq regardless, and
deepseek/gemini env fallbacks are deliberately unused. After first login
(`INITIAL_PASSWORD`), connect every provider in Dashboard → Providers:

openrouter, glm (Z.AI coding plan), deepseek, mistral, gemini, groq.

`REQUIRE_API_KEY=true` — all `/v1/*` calls need a dashboard-created API key.

## Model IDs differ from litellm

OmniRoute has no model_list file; models are auto-discovered per connected
provider and addressed as `<provider>/<model>` — e.g.
`glm/glm-5.3`, `openrouter/deepseek/deepseek-v4-flash`. These are NOT the
litellm alias strings; clients must switch to OmniRoute ids. See the mirror
table in the plan (`260821-omniroute-docker-facts` / omniroute-testing-setup).

## Promotion

If OmniRoute replaces litellm as the primary router, that is a separate
deferred to-do entry with its own pre-thought design (consumer re-pointing
incl. all hermes profiles). This branch only stages the testing deployment.
