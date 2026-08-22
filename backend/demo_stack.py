"""GeoNexus Web reference app — demo backend stack.

Boots a local demo environment so the reference app works out of the box:

1. A GeoNode with the Amazon NDVI skill (synthetic data) + its GeoCard,
   protected by an API key (node auth).
2. A shared GeoCard Registry the node advertises into.
3. The Web BFF (``geonexus.web``) wiring them together with JWT auth.
4. **MCP client demo (v1.1)**: an internal MCP HTTP server exposes extra
   tools (echo / describe), and ``MCPToolClient.http`` imports them as
   GeoSkills on the node — GeoNexus as an MCP client.
5. **Reflective goals demo (v1.1)**: without ``GEONEXUS_LLM_API_KEY`` a
   built-in mock ``/chat/completions`` endpoint drives the reflective
   executor, so the full natural-language → plan → self-heal → evaluate
   flow runs with zero external configuration.

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
from fastapi import Request

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

# MCP demo server (imported as GeoSkills via the MCP client bridge).
MCP_PORT = 9001
MCP_TOOLS = ("echo", "describe")


class RunningStack:
    """Handle trio of background uvicorn servers (registry, node, web)."""

    def __init__(
        self,
        registry_port: int = 8790,
        node_port: int = 8787,
        web_port: int = 8900,
        mcp_port: int = MCP_PORT,
    ) -> None:
        self.registry_port = registry_port
        self.node_port = node_port
        self.web_port = web_port
        self.mcp_port = mcp_port
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

    def build_mcp_server(self) -> Any:
        """An internal MCP HTTP server with extra tools, imported as GeoSkills.

        This is the MCP *server* the demo client bridge connects to. It is a
        GeoMCP server (echo/describe tools) exposed through the official MCP
        SDK's Streamable HTTP transport (``create_streamable_http_app``), so
        the client bridge speaks the standard MCP protocol.
        """
        from geonexus.geomcp import GeoMCPServer
        from geonexus.mcp_adapter import create_streamable_http_app

        server = GeoMCPServer(name="mcp-demo-server")
        server.register_tool(
            "echo",
            lambda params: {"echo": params},
            description="Echo the given params back (MCP demo tool)",
        )
        server.register_tool(
            "describe",
            lambda params: {"node": "mcp-demo-server", "params": params},
            description="Describe this MCP demo server (MCP demo tool)",
        )
        # The MCP Streamable HTTP app (served at /mcp by the SDK).
        return create_streamable_http_app(server)

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

        # MCP client demo: run an internal MCP HTTP server and import its
        # tools as GeoSkills on the demo node (v1.1).
        self._serve(self.build_mcp_server(), self.mcp_port)
        time.sleep(0.3)
        imported = self._import_mcp_tools()
        self._node.advertise(registry_url, endpoint=f"http://127.0.0.1:{self.node_port}")

        # Web BFF on top; the LLM config points at a built-in mock when no
        # real key is configured, so reflective goals run out of the box.
        config = WebConfig(
            registry_url=registry_url,
            jwt=JWTConfig(secret=WEB_SECRET),
            users={DEMO_USER: DEMO_PASSWORD},
            node_api_keys={f"http://127.0.0.1:{self.node_port}": NODE_KEY},
            default_node_url=f"http://127.0.0.1:{self.node_port}",
            llm=self._llm_config(registry_url),
        )
        self.web_app = create_web_app(config, cors_origins=["*"])
        # Demo-only mock LLM (used when no GEONEXUS_LLM_API_KEY is set).
        self.install_mock_llm(self.web_app)
        # Serve the static frontend from the same app (reference convenience).
        from fastapi.staticfiles import StaticFiles

        frontend_dir = REPO_ROOT / "frontend"
        if frontend_dir.exists():
            self.web_app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
        self._serve(self.web_app, self.web_port)

        logger.info(
            "Demo stack ready: registry=:%d node=:%d web=:%d mcp=:%d "
            "imported=%d skills (user=%s)",
            self.registry_port,
            self.node_port,
            self.web_port,
            self.mcp_port,
            len(imported),
            DEMO_USER,
        )

    # ------------------------------------------------------------------ #
    # MCP client bridge demo
    # ------------------------------------------------------------------ #
    def _import_mcp_tools(self) -> list[Any]:
        """Import the internal MCP server's tools as GeoSkills (v1.1)."""
        from geonexus.mcp_client import MCPToolClient

        url = f"http://127.0.0.1:{self.mcp_port}/mcp"
        try:
            with MCPToolClient.http(url, name="mcp-demo") as mcp:
                skills = mcp.to_skills(prefix="mcp-")
            for skill in skills:
                self._node.register_skill_object(skill)
                logger.info("Imported MCP tool as GeoSkill '%s'", skill.name)
            return skills
        except Exception as exc:  # noqa: BLE001 - demo must not crash without MCP SDK
            logger.warning("MCP import skipped (SDK missing?): %s", exc)
            return []

    # ------------------------------------------------------------------ #
    # LLM config: real endpoint when configured, else a built-in mock.
    # ------------------------------------------------------------------ #
    def _llm_config(self, registry_url: str) -> Any:
        import os

        from geonexus.agent import LLMConfig

        api_key = os.environ.get("GEONEXUS_LLM_API_KEY")
        if api_key:
            return LLMConfig.from_env()
        # Mock mode: point the planner at a local /chat/completions endpoint
        # served by the web app itself. The mock translates the request into
        # a small NDVI pipeline and always advises "retry" on failures, so
        # the reflective flow is observable without an external LLM.
        mock = LLMConfig(
            base_url=f"http://127.0.0.1:{self.web_port}",
            api_key="mock",
            model="demo-mock",
        )
        # Remember the registry URL so the mock can ground on real skills.
        self._mock_registry = registry_url
        return mock

    def install_mock_llm(self, app: Any) -> None:
        """Mount the demo /chat/completions mock on the web app."""
        registry_url = getattr(self, "_mock_registry", None) or f"http://127.0.0.1:{self.registry_port}"

        @app.post("/chat/completions")
        async def mock_chat(request: Request) -> dict[str, Any]:
            body = await request.json()
            messages = body.get("messages", [])
            user = next(
                (m["content"] for m in messages if m.get("role") == "user"), ""
            )
            system = next(
                (m["content"] for m in messages if m.get("role") == "system"), ""
            )
            # Plan review (evaluate_plan): return a canned self-assessment.
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
            # Repair-advice requests contain "Failed step"; return a retry.
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
            # Goal translation: ground on the registry's real skills.
            try:
                from geonexus.registry import RegistryClient

                with RegistryClient(registry_url, timeout=5.0) as rc:
                    names = [e["skill"]["name"] for e in rc.list_skills()]
            except Exception:  # noqa: BLE001 - fall back to a fixed skill list
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
        f"\nGeoNexus Web reference app:\n"
        f"  Web BFF:    http://127.0.0.1:{stack.web_port}\n"
        f"  Registry:   http://127.0.0.1:{stack.registry_port}\n"
        f"  Node:       http://127.0.0.1:{stack.node_port}\n"
        f"  MCP server: http://127.0.0.1:{stack.mcp_port}/mcp (imported as GeoSkills)\n"
        f"  Login:      {DEMO_USER} / {DEMO_PASSWORD}\n"
        f"  LLM:        {'real (GEONEXUS_LLM_API_KEY)' if __import__('os').environ.get('GEONEXUS_LLM_API_KEY') else 'built-in mock (set GEONEXUS_LLM_API_KEY for a real LLM)'}\n"
        f"  OpenAPI:    http://127.0.0.1:{stack.web_port}/docs\n"
        f"Press Ctrl+C to stop.\n"
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stack.stop()


if __name__ == "__main__":
    main()
