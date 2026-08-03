"""Smoke test for the built-in Swagger UI server."""

import http.server
import threading
import urllib.request

from tests.conftest import run_pipeline


def test_serve_returns_ui_and_spec(tmp_path):
    _metadata, _docs, results = run_pipeline("microservices", tmp_path)
    spec_path = results[0].output_path

    import functools

    from openapi_agent.serve.server import _DocsHandler, _swagger_ui_dir

    from pathlib import Path

    _DocsHandler.spec_path = Path(spec_path)
    _DocsHandler.spec_name = Path(spec_path).name
    handler = functools.partial(_DocsHandler, directory=str(_swagger_ui_dir()))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        index = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
        assert index.status == 200
        assert b"SwaggerUIBundle" in index.read()
        spec = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/{Path(spec_path).name}", timeout=5
        )
        assert spec.status == 200
        assert b"openapi" in spec.read()
    finally:
        server.shutdown()
