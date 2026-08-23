"""GeoNexus Web reference app — resource coordination demo.

Boots a multi-node demo environment so the reference app shows how GeoCards
coordinate **data, models and compute**:

1. **data-node-a** (:8787) — owns the 2015 Amazon scene (data GeoCard
   `amazon-ndvi-2015`) and an `ndvi` skill that computes NDVI on *its* data.
2. **data-node-b** (:8788) — owns the 2025 scene (data GeoCard
   `amazon-ndvi-2025`) and an `ndvi` skill.
3. **compute-node** (:8789) — hosts the change-detection **model** (model
   GeoCard `model-ndvi-change` with `runtime: gpu=cuda`) and is advertised
   as a compute resource (`compute-node-gpu`). A second, CPU-only compute
   card (`compute-node-cpu`) demonstrates *rejection* when a GPU is required.
4. A shared **registry** (:8790) holding the data / model / compute cards
   plus the skills, so the Web BFF can retrieve, contract-check, resource-
   match and plan across nodes.
5. The **Web BFF** (:8900) exposes a coordination endpoint
   (`POST /api/coordinate`) that runs the full GeoCard workflow and returns
   a teaching report (retrieve → contract → resource match → plan →
   execute), rendered as a timeline by the frontend.

Run ``python backend/demo_stack.py`` to bring up the whole stack.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Request
from pydantic import BaseModel

from geonexus.geocard import GeoCardBuilder
from geonexus.geomcp import GeoMCPServer
from geonexus.geonode import GeoNode, Skill
from geonexus.registry import RegistryServer
from geonexus.web import WebConfig, JWTConfig, create_web_app

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT.parent / "mvp" / "examples"

NODE_KEY = "demo-node-key"
WEB_SECRET = "demo-web-secret-0123456789abcdefghij"
DEMO_USER = "demo"
DEMO_PASSWORD = "demo1234"

MCP_PORT = 9001

# Study area (Amazon rainforest) + the two time steps.
AMAZON_BBOX = [-73.9, -15.0, -44.0, 5.0]
AMAZON_CRS = "EPSG:4326"
YEARS = (2015, 2025)


class CoordinateRequest(BaseModel):
    """Coordination request: capability + spatial/temporal scope + resources."""
    capability: str = "change-detection"
    bbox: list[float] = list(AMAZON_BBOX)
    start: str = "2015-01-01"
    end: str = "2025-12-31"
    require_gpu: bool = False


class RunningStack:
    """Multi-node demo stack: 2 data nodes + 1 compute node + registry + BFF."""

    def __init__(
        self,
        registry_port: int = 8790,
        data_a_port: int = 8787,
        data_b_port: int = 8788,
        compute_port: int = 8789,
        web_port: int = 8900,
        mcp_port: int = MCP_PORT,
    ) -> None:
        self.registry_port = registry_port
        self.data_a_port = data_a_port
        self.data_b_port = data_b_port
        self.compute_port = compute_port
        self.web_port = web_port
        self.mcp_port = mcp_port
        self._threads: list[threading.Thread] = []
        self._servers: list[uvicorn.Server] = []
        self.web_app: Any = None
        self.nodes: dict[str, GeoNode] = {}

    # ------------------------------------------------------------------ #
    # Data nodes: each owns ONE time step of the scene (data sovereignty).
    # ------------------------------------------------------------------ #
    def _build_data_node(self, year: int, port: int, node_name: str) -> GeoMCPServer:
        if str(EXAMPLES_DIR) not in sys.path:
            sys.path.insert(0, str(EXAMPLES_DIR.parent))
        from examples.amazon_ndvi import skill as ndvi_skill

        workdir = REPO_ROOT / "output" / str(year)
        workdir.mkdir(parents=True, exist_ok=True)

        red_path = workdir / f"synthetic_red_{year}.tif"
        if not red_path.exists():
            ndvi_skill.generate_synthetic_scene(
                year, str(workdir), width=160, height=160, seed=year
            )
        inputs = {
            "red": str(red_path),
            "nir": str(workdir / f"synthetic_nir_{year}.tif"),
        }

        node = GeoNode(name=node_name, workdir=str(workdir))

        def _ndvi_handler(params: dict[str, Any], context: Any) -> dict[str, Any]:
            merged = {**inputs, **params}
            return ndvi_skill.ndvi_analysis_handler(merged, context)

        node.register_skill_object(
            Skill(
                name="ndvi",
                description=f"Compute NDVI from red/NIR rasters ({year} scene).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "red": {"type": "string"},
                        "nir": {"type": "string"},
                        "output": {"type": "string"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {"ndvi_raster": {"type": "string"}, "stats": {"type": "object"}},
                },
                handler=_ndvi_handler,
            )
        )

        # The data GeoCard for this time step.
        card = (
            GeoCardBuilder(
                id=f"amazon-ndvi-{year}",
                type="data",
                name=f"Amazon NDVI {year}",
                description=f"Synthetic red/NIR scene of the Amazon ({year}).",
            )
            .tag("synthetic", "amazon", "ndvi")
            .capability("ndvi", "Red/NIR bands for NDVI computation")
            .spatial(bbox=AMAZON_BBOX, crs=AMAZON_CRS)
            .temporal(start=f"{year}-01-01", end=f"{year}-12-31")
            .access(
                protocol="file",
                endpoint=str(workdir),
                format="GeoTIFF",
            )
            .provenance(provider="synthetic", source=f"amazon-{year}", lineage=f"Synthetic {year} scene")
            .build()
        )
        node.register_geocard(card)
        self.nodes[node_name] = node

        return GeoMCPServer(
            name=node_name,
            engine=node.runtime,
            geocard_registry=node.geocard_registry,
            skill_registry=node.skill_registry,
            api_keys={NODE_KEY},
        )

    # ------------------------------------------------------------------ #
    # Compute node: hosts the change-detection model (needs GPU).
    # ------------------------------------------------------------------ #
    def _build_compute_node(self, port: int, node_name: str) -> GeoMCPServer:
        if str(EXAMPLES_DIR) not in sys.path:
            sys.path.insert(0, str(EXAMPLES_DIR.parent))
        from examples.amazon_ndvi import skill as ndvi_skill

        workdir = REPO_ROOT / "output"
        workdir.mkdir(parents=True, exist_ok=True)
        node = GeoNode(name=node_name, workdir=str(workdir))

        def _change_handler(params: dict[str, Any], context: Any) -> dict[str, Any]:
            """Change detection model: needs two NDVI rasters, runs on GPU."""
            ndvi_a = params.get("ndvi_a")
            ndvi_b = params.get("ndvi_b")
            if not ndvi_a or not ndvi_b:
                raise ValueError("ndvi-change requires 'ndvi_a' and 'ndvi_b' raster paths")
            stats = ndvi_skill.compute_change(str(ndvi_a), str(ndvi_b), str(workdir / "ndvi_change.tif"))
            return {
                "change_raster": str(workdir / "ndvi_change.tif"),
                "stats": stats,
                "synthetic": True,
            }

        node.register_skill_object(
            Skill(
                name="ndvi-change",
                description="Change-detection model: NDVI 2025 - 2015 (GPU-accelerated).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "ndvi_a": {"type": "string"},
                        "ndvi_b": {"type": "string"},
                        "output": {"type": "string"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {"change_raster": {"type": "string"}, "stats": {"type": "object"}},
                },
                handler=_change_handler,
            )
        )

        # The model GeoCard: declares its runtime requirements (needs GPU).
        model_card = (
            GeoCardBuilder(
                id="model-ndvi-change",
                type="model",
                name="NDVI Change-Detection Model",
                description="Computes NDVI difference between two time steps.",
            )
            .tag("model", "change-detection")
            .capability("change-detection", "NDVI 2025 - 2015")
            .input("ndvi_a", "raster", required=True)
            .input("ndvi_b", "raster", required=True)
            .output("change_raster", "raster")
            .output("stats", "object")
            .runtime(cpu=2, memory="4Gi", gpu="cuda")
            .interface(type="geomcp-skill", version="1.0")
            .provenance(provider="demo", source="model-ndvi-change", lineage="Demo change-detection model")
            .build()
        )
        node.register_geocard(model_card)
        self.nodes[node_name] = node

        return GeoMCPServer(
            name=node_name,
            engine=node.runtime,
            geocard_registry=node.geocard_registry,
            skill_registry=node.skill_registry,
            api_keys={NODE_KEY},
        )

    def build_registry(self) -> RegistryServer:
        return RegistryServer(name="demo-registry", api_keys=None)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        registry = self.build_registry()
        node_a = self._build_data_node(2015, self.data_a_port, "data-node-a")
        node_b = self._build_data_node(2025, self.data_b_port, "data-node-b")
        compute = self._build_compute_node(self.compute_port, "compute-node")

        self._serve(registry.create_app(), self.registry_port)
        self._serve(node_a.create_app(), self.data_a_port)
        self._serve(node_b.create_app(), self.data_b_port)
        self._serve(compute.create_app(), self.compute_port)
        time.sleep(0.5)

        registry_url = f"http://127.0.0.1:{self.registry_port}"
        # Advertise each node's cards + skills into the registry.
        self.nodes["data-node-a"].advertise(registry_url, endpoint=f"http://127.0.0.1:{self.data_a_port}")
        self.nodes["data-node-b"].advertise(registry_url, endpoint=f"http://127.0.0.1:{self.data_b_port}")
        self.nodes["compute-node"].advertise(registry_url, endpoint=f"http://127.0.0.1:{self.compute_port}")

        # Advertise the compute RESOURCES as `type: compute` cards: one with a
        # GPU (the real node) and one CPU-only, to demonstrate rejection.
        self._advertise_compute_cards(registry_url)

        # Web BFF on top (mock LLM when no real key is configured).
        config = WebConfig(
            registry_url=registry_url,
            jwt=JWTConfig(secret=WEB_SECRET),
            users={DEMO_USER: DEMO_PASSWORD},
            node_api_keys={
                f"http://127.0.0.1:{self.data_a_port}": NODE_KEY,
                f"http://127.0.0.1:{self.data_b_port}": NODE_KEY,
                f"http://127.0.0.1:{self.compute_port}": NODE_KEY,
            },
            default_node_url=f"http://127.0.0.1:{self.compute_port}",
            llm=self._llm_config(registry_url),
            # Data registration workflow: uploaded files + registry writes.
            datasets_dir=str(REPO_ROOT / "uploads"),
            registry_api_key=None,  # registry has no API key in the demo
            upload_node_url=f"http://127.0.0.1:{self.compute_port}",
        )
        self.web_app = create_web_app(config, cors_origins=["*"])
        self.install_mock_llm(self.web_app)
        self.install_coordinate_endpoint(self.web_app, registry_url)

        from fastapi.staticfiles import StaticFiles

        frontend_dir = REPO_ROOT / "frontend"
        if frontend_dir.exists():
            self.web_app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
        self._serve(self.web_app, self.web_port)

        logger.info(
            "Demo stack ready: registry=:%d data-a=:%d data-b=:%d compute=:%d web=:%d",
            self.registry_port,
            self.data_a_port,
            self.data_b_port,
            self.compute_port,
            self.web_port,
        )

    def _advertise_compute_cards(self, registry_url: str) -> None:
        """Register `type: compute` cards describing available compute."""
        from geonexus.registry import RegistryClient

        gpu_card = (
            GeoCardBuilder(
                id="compute-node-gpu",
                type="compute",
                name="GPU compute node",
                description="Compute node with an NVIDIA CUDA GPU.",
            )
            .tag("compute", "gpu")
            .capability("compute", "Runs CUDA-accelerated models")
            .runtime(cpu=8, memory="16Gi", gpu="cuda")
            .access(protocol="geomcp", endpoint=f"http://127.0.0.1:{self.compute_port}")
            .build()
        )
        cpu_card = (
            GeoCardBuilder(
                id="compute-node-cpu",
                type="compute",
                name="CPU compute node",
                description="Compute node without a GPU.",
            )
            .tag("compute", "cpu")
            .capability("compute", "Runs CPU-only models")
            .runtime(cpu=4, memory="8Gi", gpu="none")
            .access(protocol="geomcp", endpoint=f"http://127.0.0.1:{self.compute_port}")
            .build()
        )
        with RegistryClient(registry_url) as rc:
            for card in (gpu_card, cpu_card):
                try:
                    rc.register(card, node_url=card.access.endpoint or f"http://127.0.0.1:{self.compute_port}")
                except Exception as exc:  # noqa: BLE001 - already registered
                    logger.info("compute card %s: %s", card.id, exc)

    # ------------------------------------------------------------------ #
    # Coordination endpoint: retrieve -> contract -> resource -> plan -> execute
    # ------------------------------------------------------------------ #
    def install_coordinate_endpoint(self, app: Any, registry_url: str) -> None:
        """Mount POST /api/coordinate — the full GeoCard workflow demo."""
        from fastapi import APIRouter

        from geonexus.agent.planner import GeoAgentPlanner
        from geonexus.geocard import ContractValidator, GeoCard
        from geonexus.registry import RegistryClient
        from geonexus.resource import (
            ComputeCapability,
            match_resource,
            resolve_compute_capabilities,
        )

        router = APIRouter(prefix="/api")

        def _run_coordinate(body: CoordinateRequest) -> dict[str, Any]:
            report: dict[str, Any] = {
                "request": body.model_dump(),
                "steps": [],  # timeline entries
            }

            # 1. Retrieve: find data + model cards at the registry.
            with RegistryClient(registry_url) as rc:
                data_results = rc.search(type="data", bbox=body.bbox)
                model_results = rc.search(type="model", capability=body.capability)
            data_cards = []
            for r in data_results:
                entry = r.get("entry") or {}
                card = entry.get("card") or {}
                if card.get("type") == "data":
                    data_cards.append(card)
            model_cards = [
                (r.get("entry") or {}).get("card") or {} for r in model_results
            ]
            report["steps"].append(
                {
                    "phase": "retrieve",
                    "title": "检索 (Retrieve)",
                    "status": "ok" if (data_cards and model_cards) else "empty",
                    "data_cards": data_cards,
                    "model_cards": model_cards,
                }
            )

            # 2. Contract check: validate each data card against the request.
            validator = ContractValidator()
            contract_entries = []
            contract_ok = True
            for card_dict in data_cards:
                card = GeoCard.model_validate(card_dict)
                result = validator.check(
                    card,
                    bbox=body.bbox,
                    crs=AMAZON_CRS,
                    start=body.start,
                    end=body.end,
                )
                contract_entries.append(
                    {"card_id": card.id, "satisfied": result.satisfied, "reasons": result.reasons, "warnings": result.warnings}
                )
                contract_ok = contract_ok and result.satisfied
            report["steps"].append(
                {
                    "phase": "contract",
                    "title": "契约校验 (Contract)",
                    "status": "ok" if contract_ok else "fail",
                    "entries": contract_entries,
                }
            )

            # 3. Resource match: model.runtime vs compute nodes.
            model_card = model_cards[0] if model_cards else None
            resource_report: dict[str, Any] = {"matched": False, "compute_node": None, "reasons": []}
            compute_caps = resolve_compute_capabilities(registry_url)
            # Build ComputeCapability from the card's runtime directly for display.
            caps: list[ComputeCapability] = []
            for c in compute_caps:
                caps.append(c)
            if body.require_gpu:
                caps = [c for c in caps if (c.gpu or "none").lower() not in ("none", "")]
            if model_card is not None:
                mc = GeoCard.model_validate(model_card)
                match = match_resource(mc, caps)
                resource_report = match.to_dict()
            report["steps"].append(
                {
                    "phase": "resource",
                    "title": "算力匹配 (Resource)",
                    "status": "ok" if resource_report.get("matched") else "fail",
                    "match": resource_report,
                    "compute_capabilities": [c.to_dict() for c in compute_caps],
                }
            )

            # 4. Plan: NDVI per data node (pushdown) + change on compute node.
            plan_steps: list[dict[str, Any]] = []
            if contract_ok and resource_report.get("matched"):
                data_nodes = {}
                with RegistryClient(registry_url) as rc:
                    for card_dict in data_cards:
                        entry = rc.get(card_dict["id"])
                        if isinstance(entry, dict):
                            data_nodes[card_dict["id"]] = entry.get("node_url")
                ndvi_steps = []
                for i, card_dict in enumerate(data_cards, start=1):
                    ndvi_steps.append(
                        {
                            "step_id": i,
                            "skill": "ndvi",
                            "node_url": data_nodes.get(card_dict["id"], "?"),
                            "geocards": [card_dict["id"]],
                            "params": {},
                            "description": f"NDVI on {card_dict['id']}",
                            "status": "planned",
                        }
                    )
                change_step = {
                    "step_id": len(ndvi_steps) + 1,
                    "skill": "ndvi-change",
                    "node_url": resource_report.get("compute_node"),
                    "geocards": [],
                    "params": {
                        "ndvi_a": "${step1.outputs.ndvi_raster}",
                        "ndvi_b": "${step2.outputs.ndvi_raster}",
                    },
                    "depends_on": [s["step_id"] for s in ndvi_steps],
                    "description": "Change detection (GPU)",
                    "status": "planned",
                }
                plan_steps = ndvi_steps + [change_step]
            report["steps"].append(
                {
                    "phase": "plan",
                    "title": "规划 (Plan)",
                    "status": "ok" if plan_steps else "fail",
                    "steps": plan_steps,
                }
            )

            # 5. Execute via the deterministic planner (multi-node pushdown).
            execution: dict[str, Any] = {"status": "skipped"}
            if plan_steps:
                goal = {
                    "capability": body.capability,
                    "steps": [
                        {
                            "skill": s["skill"],
                            "label": s["description"],
                            "geocards": s.get("geocards", []),
                            "params": s.get("params", {}),
                            "depends_on": s.get("depends_on", []),
                        }
                        for s in plan_steps
                    ],
                }
                try:
                    from geonexus.agent import Goal

                    g = Goal(**goal)
                    # Plan through GeoAgentPlanner (resolves nodes from registry).
                    plan = GeoAgentPlanner(registry_url).plan_pipeline(g)
                    from geonexus.agent.planner import PlanExecutor

                    with PlanExecutor(registry_url, api_key=NODE_KEY) as ex:
                        plan = ex.run(plan)
                    execution = {
                        "status": "done" if not plan.failed else "partial",
                        "steps": [s.to_dict() for s in plan.steps],
                    }
                except Exception as exc:  # noqa: BLE001 - surface execution errors
                    execution = {"status": "failed", "error": str(exc)}
            report["steps"].append(
                {
                    "phase": "execute",
                    "title": "执行 (Execute)",
                    "status": execution.get("status", "skipped"),
                    "execution": execution,
                }
            )
            return report

        @router.post("/coordinate")
        def coordinate(body: CoordinateRequest) -> dict[str, Any]:
            return _run_coordinate(body)

        app.include_router(router)

    # ------------------------------------------------------------------ #
    # LLM config: real endpoint when configured, else a built-in mock.
    # ------------------------------------------------------------------ #
    def _llm_config(self, registry_url: str) -> Any:
        import os

        from geonexus.agent import LLMConfig

        api_key = os.environ.get("GEONEXUS_LLM_API_KEY")
        if api_key:
            return LLMConfig.from_env()
        mock = LLMConfig(
            base_url=f"http://127.0.0.1:{self.web_port}",
            api_key="mock",
            model="demo-mock",
        )
        self._mock_registry = registry_url
        return mock

    def install_mock_llm(self, app: Any) -> None:
        """Mount the demo /chat/completions mock on the web app."""
        registry_url = getattr(self, "_mock_registry", None) or f"http://127.0.0.1:{self.registry_port}"

        @app.post("/chat/completions")
        async def mock_chat(request: Request) -> dict[str, Any]:
            body = await request.json()
            messages = body.get("messages", [])
            user = next((m["content"] for m in messages if m.get("role") == "user"), "")
            system = next((m["content"] for m in messages if m.get("role") == "system"), "")
            if "plan reviewer" in system.lower() or "Executed plan" in user:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"satisfied": true, "score": 88, '
                                    '"notes": "mock: goal addressed, NDVI computed"}'
                                ),
                            }
                        }
                    ]
                }
            if "Failed step" in user or "repair advice" in user.lower():
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"action": "retry", "reason": "mock: retry", '
                                    '"params": {}}'
                                ),
                            }
                        }
                    ]
                }
            try:
                from geonexus.registry import RegistryClient

                with RegistryClient(registry_url, timeout=5.0) as rc:
                    names = [e["skill"]["name"] for e in rc.list_skills()]
            except Exception:  # noqa: BLE001 - fall back
                names = ["ndvi"]
            skill = names[0] if names else "ndvi"
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                '{"capability": "ndvi", '
                                f'"skill": "{skill}", '
                                '"steps": ['
                                '{"skill": "' + skill + '", "label": "compute"}'
                                "]}"
                            ),
                        }
                    }
                ]
            }

        return None

    def _serve(self, app: Any, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self._threads.append(thread)
        self._servers.append(server)

    def stop(self) -> None:
        for server in self._servers:
            server.should_exit = True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stack = RunningStack()
    stack.start()
    print(
        f"\nGeoNexus Web — resource coordination demo:\n"
        f"  data-a  : http://127.0.0.1:{stack.data_a_port}  (2015 scene)\n"
        f"  data-b  : http://127.0.0.1:{stack.data_b_port}  (2025 scene)\n"
        f"  compute : http://127.0.0.1:{stack.compute_port}  (change model, GPU)\n"
        f"  registry: http://127.0.0.1:{stack.registry_port}\n"
        f"  Web BFF : http://127.0.0.1:{stack.web_port}  (login demo / demo1234)\n"
        f"  OpenAPI : http://127.0.0.1:{stack.web_port}/docs\n"
        f"Press Ctrl+C to stop.\n"
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stack.stop()


if __name__ == "__main__":
    main()
