"""GeoNexus Web reference app — demo backend stack.

Boots a local demo environment so the reference app works out of the box:

1. A GeoNode with the Amazon NDVI skill (synthetic data) + its GeoCard,
   protected by an API key (node auth).
2. A shared GeoCard Registry the node advertises into.
3. The Web BFF (``geonexus.web``) wiring them together with JWT auth.

Run ``python backend/demo_stack.py`` to bring up the whole stack, or import
the pieces separately in your own app.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from geonexus.geocard import GeoCardBuilder, load_geocard
from geonexus.geomcp import GeoMCPServer
from geonexus.geonode import GeoNode, LocalRuntime
from geonexus.registry import RegistryServer
from geonexus.web import WebConfig, JWTConfig, create_web_app

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT.parent / "mvp" / "examples"
GEOCARD_PATH = EXAMPLES_DIR / "amazon_ndvi" / "geocard.yaml"

NODE_KEY = "demo-node-key"
WEB_SECRET = "demo-web-secret-0123456789abcdefghij"
DEMO_USER = "demo"
DEMO_PASSWORD = "demo1234"


class RunningStack:
    """Handle trio of background uvicorn servers (registry, node, web)."""

    def __init__(
        self,
        registry_port: int = 8790,
        node_port: int = 8787,
        web_port: int = 8900,
    ) -> None:
        self.registry_port = registry_port
        self.node_port = node_port
        self.web_port = web_port
        self._threads: list[threading.Thread] = []
        self._servers: list[uvicorn.Server] = []
        self.web_app: Any = None
        self._node: Any = None

    # ------------------------------------------------------------------ #
    def build_node(self) -> GeoMCPServer:
        """GeoNode with the Amazon NDVI skill and a GeoCard, key-protected."""
        if str(EXAMPLES_DIR) not in sys.path:
            sys.path.insert(0, str(EXAMPLES_DIR.parent))

        from examples.amazon_ndvi import skill as ndvi_skill

        from geonexus.geonode import Skill

        # Generate synthetic red/nir rasters once, so the demo executes
        # out of the box (the NDVI skill needs real file paths).
        workdir = REPO_ROOT / "output"
        workdir.mkdir(parents=True, exist_ok=True)
        red_path = workdir / "red.tif"
        nir_path = workdir / "nir.tif"
        if not red_path.exists():
            from geonexus.geonode import SkillContext

            paths = ndvi_skill.generate_synthetic_scene(
                2025, str(workdir), width=120, height=120, seed=42
            )
            red_path = Path(paths["red"])
            nir_path = Path(paths["nir"])
        self._demo_inputs = {"red": str(red_path), "nir": str(nir_path)}

        node = GeoNode(name="demo-node", workdir=str(workdir))

        def _ndvi_handler(params: dict[str, Any], context: Any) -> dict[str, Any]:
            """Fill in the demo rasters when the caller omits them."""
            merged = {**self._demo_inputs, **params}
            return ndvi_skill.ndvi_analysis_handler(merged, context)

        skill = Skill(
            name="ndvi",
            description="Compute NDVI from red/NIR rasters (synthetic demo data).",
            input_schema={
                "type": "object",
                # red/nir are optional here: the demo handler fills in
                # synthetic rasters when the caller omits them.
                "properties": {
                    "red": {"type": "string"},
                    "nir": {"type": "string"},
                    "output": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "ndvi_raster": {"type": "string"},
                    "stats": {"type": "object"},
                },
            },
            handler=_ndvi_handler,
        )
        node.register_skill_object(skill)

        if GEOCARD_PATH.exists():
            card = load_geocard(str(GEOCARD_PATH))
        else:
            card = (
                GeoCardBuilder("amazon-ndvi-demo", "demo")
                .spatial(bbox=[-73.9, -15.0, -44.0, 5.0], crs="EPSG:4326")
                .build()
            )
        node.register_geocard(card)

        server = GeoMCPServer(
            name="demo-node",
            engine=node.runtime,
            geocard_registry=node.geocard_registry,
            skill_registry=node.skill_registry,
            api_keys={NODE_KEY},
        )
        # Remember the node so we can advertise it after the HTTP server is up.
        self._node = node
        return server

    def build_registry(self) -> RegistryServer:
        return RegistryServer(name="demo-registry", api_keys=None)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._node = None
        registry = self.build_registry()
        node_server = self.build_node()
        self._serve(registry.create_app(), self.registry_port)
        self._serve(node_server.create_app(), self.node_port)
        # Wait for the node, then advertise its cards + skills into the registry.
        time.sleep(0.5)
        registry_url = f"http://127.0.0.1:{self.registry_port}"
        self._node.advertise(registry_url, endpoint=f"http://127.0.0.1:{self.node_port}")

        # Web BFF on top.
        config = WebConfig(
            registry_url=f"http://127.0.0.1:{self.registry_port}",
            jwt=JWTConfig(secret=WEB_SECRET),
            users={DEMO_USER: DEMO_PASSWORD},
            node_api_keys={f"http://127.0.0.1:{self.node_port}": NODE_KEY},
            default_node_url=f"http://127.0.0.1:{self.node_port}",
        )
        self.web_app = create_web_app(config, cors_origins=["*"])
        # Serve the static frontend from the same app (reference convenience).
        from fastapi.staticfiles import StaticFiles

        frontend_dir = REPO_ROOT / "frontend"
        if frontend_dir.exists():
            self.web_app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
        self._serve(self.web_app, self.web_port)

        logger.info(
            "Demo stack ready: registry=:%d node=:%d web=:%d (user=%s)",
            self.registry_port,
            self.node_port,
            self.web_port,
            DEMO_USER,
        )

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
        f"\nGeoNexus Web reference app:\n"
        f"  Web BFF:   http://127.0.0.1:{stack.web_port}\n"
        f"  Registry:  http://127.0.0.1:{stack.registry_port}\n"
        f"  Node:      http://127.0.0.1:{stack.node_port}\n"
        f"  Login:     {DEMO_USER} / {DEMO_PASSWORD}\n"
        f"  OpenAPI:   http://127.0.0.1:{stack.web_port}/docs\n"
        f"Press Ctrl+C to stop.\n"
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stack.stop()


if __name__ == "__main__":
    main()
