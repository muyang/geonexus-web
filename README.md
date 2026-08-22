# GeoNexus Web — Reference Application

A complete, runnable example of building a **Web application on top of the
GeoNexus SDK** (`geonexus-sdk` on PyPI). It demonstrates the full call chain:

```
Browser ──JWT──▶ Web BFF (geonexus.web) ──X-API-Key──▶ GeoNode (NDVI skill)
                        │                                     │
                        └────────── GeoCard Registry ─────────┘
```

- **Data**: the demo node owns an Amazon NDVI GeoCard (synthetic data),
  advertised into a shared GeoCard Registry.
- **GIS tool**: the `ndvi` GeoSkill (compute NDVI from red + nir bands).
- **Orchestration**: `geonexus.web` runs executions as async tasks with SSE
  progress; a natural-language goal endpoint demonstrates LLM planning.
- **Auth**: BFF + JWT (browser holds only a JWT; node API keys stay server
  side, forwarded as `X-API-Key`).

## Quick start

```bash
bash scripts/dev.sh
# → registry :8790, node :8787, web :8900
```

Then open **http://127.0.0.1:8900** — a single-page demo UI with four steps:

1. **登录** — `demo` / `demo1234` → JWT
2. **搜索** — query the registry (`q=ndvi`) or list skills
3. **执行** — run the `ndvi` skill as an async task with SSE progress
4. **目标** — submit a natural-language goal (needs LLM config)

API docs (OpenAPI): http://127.0.0.1:8900/docs

## What it shows

| Concern | Where in the stack |
|---|---|
| JWT login | `POST /api/auth/login` → Bearer token |
| Discovery | `GET /api/cards?q=`, `GET /api/skills`, `GET /api/nodes` |
| Async execution | `POST /api/execute` → `202 {task_id}`, poll `/api/tasks/{id}` |
| Live progress | `GET /api/tasks/{id}/stream` (SSE) |
| LLM orchestration | `POST /api/goals` (plans + runs a DAG via the SDK) |
| Node auth | node requires `X-API-Key`; BFF forwards it (`node_api_keys`) |
| Distributed auth | `JWTConfig(secret=...)` HS256 for one domain; RS256 key pair for cross-domain |

## Repository layout

```
backend/demo_stack.py   demo environment: registry + node + web BFF
frontend/index.html     single-page reference UI (vanilla JS, no build step)
scripts/dev.sh          one-command bring-up
requirements.txt        runtime deps (geonexus-sdk, uvicorn)
```

## Using it as a template

The demo stack is intentionally small. To build your own app:

1. Replace `build_node()` with your GeoNodes (your data + your skills).
2. Replace the `users` dict with OIDC/SSO at the login endpoint.
3. Put a real `JWTConfig` secret (or RS256 keys) in `WebConfig`.
4. Point `registry_url` at your federated registries.

See the SDK's `docs/WEB.md` for the full Web-layer reference (endpoints,
auth model, deployment topologies).
