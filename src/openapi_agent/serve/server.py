"""Local Swagger UI for the generated document (``openapi_agent serve``).

Serves the bundled swagger-ui-dist assets (via the ``swagger_ui_bundle``
package — no CDN, works offline) plus the generated spec on a small stdlib
HTTP server. "Try it out" requests go to the servers[] URL inside the spec
(OPENAPI_SERVER_URL), so the target API must be running and CORS-enabled for
in-browser calls.
"""

from __future__ import annotations

import functools
import http.server
from pathlib import Path

from rich.console import Console

from openapi_agent.logging_utils import get_logger

log = get_logger("serve")

_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>API Documentation</title>
  <link rel="stylesheet" href="swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="swagger-ui-bundle.js"></script>
<script src="swagger-ui-standalone-preset.js"></script>
<script>
window.onload = function() {
  window.ui = SwaggerUIBundle({
    url: "%SPEC%",
    dom_id: "#swagger-ui",
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    layout: "StandaloneLayout",
    deepLinking: true,
    displayRequestDuration: true
  });
};
</script>
</body>
</html>
"""


def _swagger_ui_dir() -> Path:
    # Prefer a project-local Swagger UI 5 build (``vendor/swagger-ui``): the
    # ``swagger-ui-bundle`` pip package pins Swagger UI 4.15.5, which predates
    # OpenAPI 3.1 support and renders our 3.1.0 specs as "does not specify a
    # valid version field". Swagger UI >= 5.0 is required for 3.1.
    vendored = Path(__file__).resolve().parents[3] / "vendor" / "swagger-ui"
    if (vendored / "swagger-ui-bundle.js").is_file():
        return vendored
    try:
        import swagger_ui_bundle

        for attribute in ("swagger_ui_path", "swagger_ui_3_path"):
            path = getattr(swagger_ui_bundle, attribute, None)
            if path:
                return Path(str(path))
    except ImportError:
        pass
    raise RuntimeError(
        "No Swagger UI assets found. Add a Swagger UI >= 5 build at "
        "vendor/swagger-ui (npm pack swagger-ui-dist@5) — required for "
        "OpenAPI 3.1 — or `pip install swagger-ui-bundle` (3.0 only)."
    )


class _DocsHandler(http.server.SimpleHTTPRequestHandler):
    """Serves swagger-ui assets from the bundle dir; overrides index + spec."""

    spec_path: Path
    spec_name: str

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path in ("/", "/index.html"):
            body = _INDEX_TEMPLATE.replace("%SPEC%", self.spec_name).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == f"/{self.spec_name}":
            try:
                body = self.spec_path.read_bytes()
            except OSError:
                self.send_error(404, "spec not found")
                return
            content_type = (
                "application/json" if self.spec_name.endswith(".json") else "application/yaml"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log.debug("http: " + format, *args)


def serve_docs(spec_path: Path, port: int, host: str = "127.0.0.1", console: Console | None = None) -> None:
    console = console or Console()
    spec_path = Path(spec_path).resolve()
    if not spec_path.is_file():
        raise FileNotFoundError(
            f"OpenAPI document not found: {spec_path} — run `openapi_agent run` first"
        )
    ui_dir = _swagger_ui_dir()
    handler = functools.partial(_DocsHandler, directory=str(ui_dir))
    handler.spec_path = spec_path  # type: ignore[attr-defined]
    handler.spec_name = spec_path.name  # type: ignore[attr-defined]
    _DocsHandler.spec_path = spec_path
    _DocsHandler.spec_name = spec_path.name

    with http.server.ThreadingHTTPServer((host, port), handler) as httpd:
        console.print(
            f"[green]Serving[/green] {spec_path.name} at [bold]http://{host}:{port}[/bold] "
            "(Ctrl+C to stop)"
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("\nstopped")
