"""Repository pre-scan: file inventory, manifests, framework signals.

Builds :class:`RepoFacts` — the cheap, parse-free snapshot that adapter
``can_handle`` scoring and language detection run against. Heavy parsing
(tree-sitter/libcst/sidecar) happens later, only for activated adapters.

This module never executes target code and never follows symlinks out of the
repository.
"""

from __future__ import annotations

import configparser
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from openapi_agent.config.loader import AgentConfig
from openapi_agent.logging_utils import get_logger

log = get_logger("detection.repo")

_MAX_SIGNAL_BYTES = 262_144  # per-file cap for the text signal scan

_PY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
_JAVA_ANNOTATION_RE = re.compile(
    r"@(RestController|Controller|RequestMapping|GetMapping|PostMapping|PutMapping|"
    r"DeleteMapping|PatchMapping|Path|GET|POST|PUT|DELETE|ApplicationPath|"
    r"ControllerAdvice|RestControllerAdvice|EnableWebFlux|SpringBootApplication|"
    r"PreAuthorize|RolesAllowed)\b"
)
_ROUTER_FUNCTION_RE = re.compile(r"\bRouterFunction\s*<")

#: Java import prefixes worth tracking (framework signals).
_JAVA_SIGNAL_PREFIXES = (
    "org.springframework.web.reactive",
    "org.springframework.web",
    "org.springframework.security",
    "org.springframework",
    "javax.ws.rs",
    "jakarta.ws.rs",
    "javax.validation",
    "jakarta.validation",
    "com.fasterxml.jackson",
    "io.micronaut",
    "io.quarkus",
)


class ManifestInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str  # repo-relative POSIX
    kind: Literal[
        "pyproject",
        "requirements",
        "setup_cfg",
        "pipfile",
        "pom",
        "gradle",
        "gradle_settings",
        "dockerfile",
        "compose",
    ]
    dependencies: dict[str, str] = Field(default_factory=dict)  # name -> version spec ('' unknown)
    modules: list[str] = Field(default_factory=list)  # maven <modules> / gradle include / compose contexts
    artifact_id: str | None = None
    packaging: str | None = None  # maven packaging (pom => aggregator)


class RepoFacts(BaseModel):
    """Internal snapshot — never serialized into the metadata document."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    root: Path
    file_counts_by_ext: dict[str, int] = Field(default_factory=dict)
    python_files: list[str] = Field(default_factory=list)  # repo-relative POSIX
    java_files: list[str] = Field(default_factory=list)
    manifests: list[ManifestInfo] = Field(default_factory=list)
    import_hits: dict[str, list[str]] = Field(default_factory=dict)  # signal -> files
    annotation_hits: dict[str, list[str]] = Field(default_factory=dict)
    config_files: list[str] = Field(default_factory=list)  # settings.py, urls.py, application.yml...

    def manifest_dep_names(self) -> set[str]:
        names: set[str] = set()
        for manifest in self.manifests:
            names.update(manifest.dependencies)
        return names

    def dep_version(self, name: str) -> str | None:
        for manifest in self.manifests:
            if name in manifest.dependencies:
                return manifest.dependencies[name] or None
        return None


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# Test source roots that must never be scanned for production endpoints.
# Java/Maven/Gradle keep tests under ``src/test`` (and integration tests under
# ``src/it`` / ``src/integration-test``); Python projects conventionally use a
# top-level or per-package ``tests`` directory. Test-only controllers (e.g.
# ``@RestController`` classes declared inside a *Test class, or FastAPI test
# apps) would otherwise be emitted as real endpoints.
_JAVA_TEST_ROOTS = frozenset({"test", "it", "integration-test", "integrationTest"})
_TEST_DIR_NAMES = frozenset({"tests", "testing"})


def _is_test_dir(entry: Path) -> bool:
    name = entry.name
    parent = entry.parent.name
    if parent == "src" and name in _JAVA_TEST_ROOTS:
        return True  # .../src/test, .../src/it (Maven/Gradle test roots)
    if name in _TEST_DIR_NAMES:
        return True  # tests/ (pytest/unittest convention)
    return False


def _iter_files(root: Path, exclude_dirs: frozenset[str]):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            log.debug("skipping unreadable dir %s: %s", current, exc)
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                # dot-dirs are never source roots; explicit excludes win
                if entry.name in exclude_dirs or entry.name.startswith("."):
                    continue
                if _is_test_dir(entry):
                    log.debug("skipping test source root %s", entry)
                    continue
                stack.append(entry)
            elif entry.is_file():
                yield entry


# ---------------------------------------------------------------------------
# Manifest parsers (best-effort; a broken manifest yields an empty record)
# ---------------------------------------------------------------------------

_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*([=<>!~;].*)?$")
_GRADLE_DEP_RE = re.compile(
    r"""(?:implementation|api|compile|runtimeOnly|compileOnly|testImplementation)\s*
        [\s(]*['"]([A-Za-z0-9_.\-]+):([A-Za-z0-9_.\-]+)(?::([A-Za-z0-9_.\-]+))?['"]""",
    re.VERBOSE,
)
_GRADLE_INCLUDE_RE = re.compile(r"""include\s*[\s(]*['"]:?([A-Za-z0-9_.:\-]+)['"]""")


def _parse_pyproject(path: Path, rel: str) -> ManifestInfo:
    deps: dict[str, str] = {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
        project = data.get("project", {})
        for spec in project.get("dependencies", []) or []:
            match = _REQ_LINE_RE.match(spec)
            if match:
                deps[match.group(1).lower().replace("_", "-")] = (match.group(3) or "").strip()
        poetry = data.get("tool", {}).get("poetry", {})
        for name, spec in (poetry.get("dependencies") or {}).items():
            if name.lower() != "python":
                deps[name.lower().replace("_", "-")] = spec if isinstance(spec, str) else ""
    except Exception as exc:  # noqa: BLE001 - malformed manifest is a signal gap, not a crash
        log.debug("failed to parse %s: %s", rel, exc)
    return ManifestInfo(path=rel, kind="pyproject", dependencies=deps)


def _parse_requirements(path: Path, rel: str) -> ManifestInfo:
    deps: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = _REQ_LINE_RE.match(line)
            if match:
                deps[match.group(1).lower().replace("_", "-")] = (match.group(3) or "").strip()
    except OSError as exc:
        log.debug("failed to read %s: %s", rel, exc)
    return ManifestInfo(path=rel, kind="requirements", dependencies=deps)


def _parse_setup_cfg(path: Path, rel: str) -> ManifestInfo:
    deps: dict[str, str] = {}
    try:
        parser = configparser.ConfigParser()
        parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
        raw = parser.get("options", "install_requires", fallback="")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            match = _REQ_LINE_RE.match(line)
            if match:
                deps[match.group(1).lower().replace("_", "-")] = (match.group(3) or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("failed to parse %s: %s", rel, exc)
    return ManifestInfo(path=rel, kind="setup_cfg", dependencies=deps)


_POM_NS = "{http://maven.apache.org/POM/4.0.0}"


def _parse_pom(path: Path, rel: str) -> ManifestInfo:
    deps: dict[str, str] = {}
    modules: list[str] = []
    artifact_id: str | None = None
    packaging: str | None = None
    try:
        tree = ET.parse(path)  # noqa: S314 - trusted local file, defusedxml not needed for offline analysis
        root = tree.getroot()

        def find(elem, tag):
            node = elem.find(f"{_POM_NS}{tag}")
            if node is None:
                node = elem.find(tag)
            return node

        def findall(elem, tag):
            return elem.findall(f"{_POM_NS}{tag}") or elem.findall(tag)

        artifact_node = find(root, "artifactId")
        artifact_id = artifact_node.text.strip() if artifact_node is not None and artifact_node.text else None
        packaging_node = find(root, "packaging")
        packaging = packaging_node.text.strip() if packaging_node is not None and packaging_node.text else None
        modules_node = find(root, "modules")
        if modules_node is not None:
            for module in findall(modules_node, "module"):
                if module.text:
                    modules.append(module.text.strip())
        deps_node = find(root, "dependencies")
        if deps_node is not None:
            for dep in findall(deps_node, "dependency"):
                group = find(dep, "groupId")
                artifact = find(dep, "artifactId")
                version = find(dep, "version")
                if group is not None and artifact is not None and group.text and artifact.text:
                    key = f"{group.text.strip()}:{artifact.text.strip()}"
                    deps[key] = version.text.strip() if version is not None and version.text else ""
        parent_node = find(root, "parent")
        if parent_node is not None:
            group = find(parent_node, "groupId")
            artifact = find(parent_node, "artifactId")
            version = find(parent_node, "version")
            if group is not None and artifact is not None and group.text and artifact.text:
                key = f"parent:{group.text.strip()}:{artifact.text.strip()}"
                deps[key] = version.text.strip() if version is not None and version.text else ""
    except Exception as exc:  # noqa: BLE001
        log.debug("failed to parse %s: %s", rel, exc)
    return ManifestInfo(
        path=rel, kind="pom", dependencies=deps, modules=modules,
        artifact_id=artifact_id, packaging=packaging,
    )


def _parse_gradle(path: Path, rel: str) -> ManifestInfo:
    deps: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _GRADLE_DEP_RE.finditer(text):
            deps[f"{match.group(1)}:{match.group(2)}"] = match.group(3) or ""
    except OSError as exc:
        log.debug("failed to read %s: %s", rel, exc)
    return ManifestInfo(path=rel, kind="gradle", dependencies=deps)


def _parse_gradle_settings(path: Path, rel: str) -> ManifestInfo:
    modules: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _GRADLE_INCLUDE_RE.finditer(text):
            modules.append(match.group(1).replace(":", "/"))
    except OSError as exc:
        log.debug("failed to read %s: %s", rel, exc)
    return ManifestInfo(path=rel, kind="gradle_settings", modules=modules)


def _parse_compose(path: Path, rel: str) -> ManifestInfo:
    modules: list[str] = []
    try:
        from ruamel.yaml import YAML

        data = YAML(typ="safe").load(path.read_text(encoding="utf-8", errors="replace"))
        for service in (data or {}).get("services", {}).values() or []:
            build = service.get("build") if isinstance(service, dict) else None
            if isinstance(build, str):
                modules.append(build)
            elif isinstance(build, dict) and "context" in build:
                modules.append(str(build["context"]))
    except Exception as exc:  # noqa: BLE001
        log.debug("failed to parse %s: %s", rel, exc)
    return ManifestInfo(path=rel, kind="compose", modules=modules)


_MANIFEST_DISPATCH = {
    "pyproject.toml": _parse_pyproject,
    "requirements.txt": _parse_requirements,
    "requirements-dev.txt": _parse_requirements,
    "setup.cfg": _parse_setup_cfg,
    "pom.xml": _parse_pom,
    "build.gradle": _parse_gradle,
    "build.gradle.kts": _parse_gradle,
    "settings.gradle": _parse_gradle_settings,
    "settings.gradle.kts": _parse_gradle_settings,
    "docker-compose.yml": _parse_compose,
    "docker-compose.yaml": _parse_compose,
    "compose.yml": _parse_compose,
    "compose.yaml": _parse_compose,
}

_PY_SIGNALS = (
    "fastapi",
    "flask",
    "django",
    "rest_framework",
    "starlette",
    "aiohttp",
    "litestar",
    "pydantic",
)

_CONFIG_BASENAMES = (
    "settings.py",
    "urls.py",
    "wsgi.py",
    "asgi.py",
    "manage.py",
    "application.yml",
    "application.yaml",
    "application.properties",
    "web.xml",
)


def build_repo_facts(config: AgentConfig) -> RepoFacts:
    root = config.project_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"project_root does not exist or is not a directory: {root}")
    exclude = config.analysis.effective_exclude_dirs()

    facts = RepoFacts(root=root)
    import_hits: dict[str, set[str]] = {}
    annotation_hits: dict[str, set[str]] = {}

    for path in _iter_files(root, exclude):
        rel = _rel(root, path)
        ext = path.suffix.lower()
        facts.file_counts_by_ext[ext] = facts.file_counts_by_ext.get(ext, 0) + 1

        name = path.name
        if name in _MANIFEST_DISPATCH:
            facts.manifests.append(_MANIFEST_DISPATCH[name](path, rel))
        elif name == "Dockerfile" or name.startswith("Dockerfile."):
            facts.manifests.append(ManifestInfo(path=rel, kind="dockerfile"))
        if name in _CONFIG_BASENAMES:
            facts.config_files.append(rel)

        if ext == ".py":
            facts.python_files.append(rel)
            _scan_python_signals(path, rel, import_hits)
        elif ext == ".java":
            facts.java_files.append(rel)
            _scan_java_signals(path, rel, import_hits, annotation_hits)

    facts.python_files.sort()
    facts.java_files.sort()
    facts.config_files.sort()
    facts.manifests.sort(key=lambda m: m.path)
    facts.import_hits = {k: sorted(v) for k, v in sorted(import_hits.items())}
    facts.annotation_hits = {k: sorted(v) for k, v in sorted(annotation_hits.items())}
    log.info(
        "pre-scan: %d python files, %d java files, %d manifests",
        len(facts.python_files), len(facts.java_files), len(facts.manifests),
    )
    return facts


def _read_signal_text(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_SIGNAL_BYTES)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _scan_python_signals(path: Path, rel: str, import_hits: dict[str, set[str]]) -> None:
    text = _read_signal_text(path)
    if not text:
        return
    found = set(_PY_IMPORT_RE.findall(text))
    for signal in _PY_SIGNALS:
        if signal in found:
            import_hits.setdefault(signal, set()).add(rel)


def _scan_java_signals(
    path: Path,
    rel: str,
    import_hits: dict[str, set[str]],
    annotation_hits: dict[str, set[str]],
) -> None:
    text = _read_signal_text(path)
    if not text:
        return
    for imported in _JAVA_IMPORT_RE.findall(text):
        for prefix in _JAVA_SIGNAL_PREFIXES:
            if imported.startswith(prefix):
                import_hits.setdefault(prefix, set()).add(rel)
                break
    for match in _JAVA_ANNOTATION_RE.finditer(text):
        annotation_hits.setdefault(f"@{match.group(1)}", set()).add(rel)
    if _ROUTER_FUNCTION_RE.search(text):
        annotation_hits.setdefault("RouterFunction", set()).add(rel)
