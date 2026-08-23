# GeoNexus Web — GeoCard Resource Coordination Demo

A web application that shows **how GeoCards work**: retrieving, matching and
coordinating **data, models and compute** across multiple nodes, with a
teaching timeline of every step.

```
Browser ──JWT──▶ Web BFF ──▶ Registry (data/model/compute GeoCards)
                                  │
      data-node-a :8787 (2015 scene) · data-node-b :8788 (2025 scene)
                                  │
      compute-node :8789 (change-detection model, GPU)
```

## The GeoCard workflow (what the app shows)

| Step | What happens | Teaching point |
|---|---|---|
| ① 检索 Retrieve | registry `search(type=...)` finds data cards (2015/2025) + model card (change-detection) | cards are discoverable by type/capability/bbox/time |
| ② 契约 Contract | `ContractValidator` checks bbox/CRS/temporal for each data card | a card must *satisfy* the request, with reasons |
| ③ 算力 Resource | `match_resource(model.runtime, compute cards)` — model needs `gpu=cuda` → routed to GPU node | models declare runtime; compute nodes advertise capacity |
| ④ 规划 Plan | NDVI pushdown to each data node; change runs on the compute node | data stays where it is; computation moves |
| ⑤ 执行 Execute | multi-node execution via the deterministic planner | results return to the caller |

Every timeline step is expandable to the raw GeoCard JSON (`access`,
`runtime`, `provenance`, …) so the cards' role in coordination is visible.

## Quick start

```bash
bash scripts/dev.sh
# → registry :8790 · data-a :8787 · data-b :8788 · compute :8789 · web :8900
open http://127.0.0.1:8900     # login demo / demo1234
```

Pick a request (capability, time window, GPU requirement), run it, and watch
the coordination timeline. Try changing **资源要求 to 不限** or a bbox
outside the study area to see the rejection path (contract / resource
mismatch).

## Data registration & review (v1.1)

The UI also demonstrates the **data registration workflow**: upload a
GeoTIFF → the backend auto-extracts metadata and generates a draft GeoCard
(`geonexus.metadata`) → submit for review (registered as `pending`, hidden
from discovery) → approve → the card becomes searchable and executable via
GeoMCP. Reject keeps it hidden with a reviewer note.

```
POST /api/datasets/upload           (multipart file → draft GeoCard)
POST /api/datasets/{id}/submit      (draft → pending)
GET  /api/datasets/pending          (review queue)
POST /api/datasets/{id}/approve|reject
```

## API

- `POST /api/coordinate` — run the full GeoCard workflow:
  `{capability, bbox, start, end, require_gpu}` → report with `steps[]`
  (retrieve / contract / resource / plan / execute).
- Standard BFF endpoints (`/api/auth/login`, `/api/cards`, `/api/execute`,
  `/api/goals`, `/api/datasets/*`, …) remain available.

## Architecture

- **data-node-a / data-node-b**: each owns one time step of the synthetic
  Amazon scene and an `ndvi` skill — NDVI is computed *on the data's node*
  (data sovereignty).
- **compute-node**: hosts the `ndvi-change` model (declares
  `runtime: gpu=cuda`); advertised as both a GPU and a CPU compute resource
  to demonstrate resource matching / rejection.
- **registry**: holds `data`, `model` and `compute` GeoCards + skills.

SDK modules used: `geonexus.resource` (new in v1.1), `geonexus.agent`
(planning/execution), `geonexus.web` (BFF + JWT), `geonexus.geocard`
(ContractValidator).
