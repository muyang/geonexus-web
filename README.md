# GeoNexus Web — Amazon Vegetation Change Analysis

A complete **web application case** built on the GeoNexus SDK
(`geonexus-sdk` on PyPI): analyse vegetation change in the Amazon rainforest
(2015 healthy → 2025 degraded) through the full stack:

```
Browser (Leaflet map) ──JWT──▶ Web BFF (geonexus.web)
                                   │  X-API-Key
                                   ▼
        GeoNode: ndvi (single-period) + ndvi-change (dual-period) skills
                                   │
                            GeoCard Registry
```

## What the app does

1. **Map** — Leaflet map showing the Amazon study area.
2. **Analyse** — pick a scenario and run it as an async task:
   - **NDVI · 2015** (healthy forest) / **NDVI · 2025** (degraded forest)
   - **Change detection · 2015 → 2025** (NDVI difference + degradation stats)
3. **Visualise** — NDVI statistics (mean / median / std), degradation pixel
   counts, and study-area layers drawn back on the map.

Verified end-to-end (synthetic data): 2015 mean NDVI **0.749** → 2025 mean
**0.318**, change **-0.43**, all pixels degraded — the app demonstrates a
realistic deforestation signal through the real GeoCard → GeoMCP → GeoNode →
GeoSkill path.

## Quick start

```bash
bash scripts/dev.sh
# → registry :8790, node :8787, web :8900, MCP server :9001
open http://127.0.0.1:8900     # login demo / demo1234
```

## Under the hood

| Layer | What it does |
|---|---|
| `geonexus.web` | JWT auth, async tasks (`/api/execute` → poll `/api/tasks/{id}`), SSE streams |
| GeoNode | `ndvi` skill (per-year synthetic red/nir rasters) + `ndvi-change` skill (2015→2025 difference, degraded-pixel count) |
| MCP client (v1.1) | internal MCP HTTP server (`:9001`) imported as `mcp-*` GeoSkills |
| Reflective goals (v1.1) | natural-language goal → plan → self-heal → `evaluation` (built-in mock LLM unless `GEONEXUS_LLM_API_KEY` is set) |
| Auth | BFF + JWT; node API keys stay server-side, forwarded as `X-API-Key` |

## Repository layout

```
backend/demo_stack.py   stack: registry + node (ndvi/ndvi-change) + web BFF + MCP server + mock LLM
frontend/index.html     Leaflet single-page application (vanilla JS, no build step)
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
