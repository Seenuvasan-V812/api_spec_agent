"""Atomic OpenAPI document writer.

ruamel.yaml (YAML 1.2, stable key order as inserted by the deterministic
builder) or canonical JSON. Content is written to a temp file in the target
directory, syntax-validated by round-trip, then atomically moved into place
with ``os.replace`` — a crash never leaves a half-written document.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from openapi_agent.logging_utils import get_logger

log = get_logger("openapi.writer")


def _make_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.version = (1, 2)
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.width = 100_000  # never wrap long strings mid-token
    yaml.representer.add_representer(
        type(None), lambda dumper, _: dumper.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return yaml


def dump_document(document: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    buffer = io.StringIO()
    yaml = _make_yaml()
    yaml.dump(document, buffer)
    text = buffer.getvalue()
    # ruamel emits a %YAML 1.2 directive + '---' when version is set; most
    # OpenAPI tooling prefers a bare document.
    if text.startswith("%YAML 1.2"):
        text = text.split("\n", 2)[-1] if "---" in text.split("\n", 2)[1] else text
    return text


def _roundtrip_ok(content: str, fmt: str) -> bool:
    try:
        if fmt == "json":
            json.loads(content)
        else:
            YAML(typ="safe").load(content)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("serialized document failed round-trip: %s", exc)
        return False


def write_document(document: dict[str, Any], path: Path, fmt: str) -> Path:
    """Serialize, verify round-trip, atomically replace. Returns final path."""
    path = Path(path)
    if fmt == "json" and path.suffix in (".yaml", ".yml"):
        path = path.with_suffix(".json")
    elif fmt == "yaml" and path.suffix == ".json":
        path = path.with_suffix(".yaml")
    content = dump_document(document, fmt)
    if not _roundtrip_ok(content, fmt):
        raise RuntimeError("serialized OpenAPI document failed syntax round-trip; not writing")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
