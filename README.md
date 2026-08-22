# GeoNexus Web — Reference Application

A complete, runnable example of building a **Web application on top of the
GeoNexus SDK** (`geonexus-sdk` on PyPI). It demonstrates the full call chain:

```
Browser ──JWT──▶ Web BFF (geonexus.web) ──X-API-Key──▶ GeoNode (NDVI skill)
                        │                                    │
                        ├───────── GeoCard Registry ─────────┤
                        └──▶ MCP client ──▶ MCP server (imported GeoSkills)
```

- **Data**: the demo node owns an Amazon NDVI GeoCard (synthetic data),
  advertised into a shared GeoCard Registry.
- **GIS tool**: the `ndvi` GeoSkill (compute NDVI from red + nir bands).
- **MCP client (v1.1)**: an internal MCP HTTP server (`:9001`) exposes
  `echo`/`describe` tools; `MCPToolClient.http` imports them as GeoSkills
  (`mcp-*`) on the node — GeoNexus as an MCP client.
- **Reflective goals (v1.1)**: natural language → registry-grounded plan →
  `ReflectiveExecutor` (LLM repairs failed steps) → `evaluate_plan`
  self-assessment. Without `GEONEXUS_LLM_API_KEY` a built-in mock LLM drives
  the flow; set the env var to use a real OpenAI-compatible endpoint.
- **Auth**: BFF + JWT (browser holds only a JWT; node API keys stay server
  side, forwarded as `X-API-Key`).

## Quick start

```bash
bash scripts/dev.sh
# → registry :8790, node :8787, web :8900, MCP server :9001
```

Then open **http://127.0.0.1:8900** — a single-page demo UI with five steps:

1. **登录** — `demo` / `demo1234` → JWT
2. **搜索** — query the registry (`q=ndvi`) or list skills
3. **执行** — run the `ndvi` skill as an async task with SSE progress
4. **目标** — submit a natural-language goal; watch reflection + evaluation
   (toggle 反射执行 on/off)
5. **MCP 导入** — list and execute the MCP-imported `mcp-*` skills

API docs (OpenAPI): http://127.0.0.1:8900/docs

## What it shows

| Concern | Where in the stack |
|---|---|
| JWT login | `POST /api/auth/login` → Bearer token |
| Discovery | `GET /api/cards?q=`, `GET /api/skills`, `GET /api/nodes` |
| Async execution | `POST /api/execute` → `202 {task_id}`, poll `/api/tasks/{id}` |
| Live progress | `GET /api/tasks/{id}/stream` (SSE) |
| Reflective goals | `POST /api/goals` — registry-grounded plan → reflective execution → `evaluation` (`{satisfied, score, notes}`) |
| MCP client bridge | internal MCP HTTP server imported as GeoSkills via `geonexus.mcp_client` |
| Node auth | node requires `X-API-Key`; BFF forwards it (`node_api_keys`) |
| Distributed auth | `JWTConfig(secret=...)` HS256 for one domain; RS256 key pair for cross-domain |

## Repository layout

```
backend/demo_stack.py   demo environment: registry + node + web BFF + MCP server + mock LLM
frontend/index.html     single-page reference UI (vanilla JS, no build step)
scripts/dev.sh          one-command bring-up
requirements.txt        runtime deps (geonexus-sdk[mcp], uvicorn)
```

## Using it as a template

The demo stack is intentionally small. To build your own app:

1. Replace `build_node()` with your GeoNodes (your data + your skills).
2. Replace the `users` dict with OIDC/SSO at the login endpoint.
3. Put a real `JWTConfig` secret (or RS256 keys) in `WebConfig`.
4. Point `registry_url` at your federated registries.
5. Replace the built-in mock LLM / demo MCP server with real endpoints.

See the SDK's `docs/WEB.md` for the full Web-layer reference (endpoints,
auth model, deployment topologies), `docs/AGENT.md` for reflective
execution, and `docs/MCP.md` for the MCP client bridge.
