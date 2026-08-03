"""Language decision: manifests + source signatures, with a single user prompt
as the tie-breaker when detection is genuinely ambiguous."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from openapi_agent.detection.repo import RepoFacts
from openapi_agent.logging_utils import get_logger

log = get_logger("detection.language")

_PY_FRAMEWORK_DEPS = {"fastapi", "flask", "django", "djangorestframework"}
_JAVA_WEB_DEP_HINTS = (
    "org.springframework.boot:spring-boot-starter-web",
    "org.springframework.boot:spring-boot-starter-webflux",
    "org.springframework:spring-webmvc",
    "org.springframework:spring-webflux",
)


@dataclass(frozen=True)
class LanguageDecision:
    languages: list[str]  # analysis order
    ambiguous: bool
    rationale: str


def decide_language(facts: RepoFacts, forced: str | None = None) -> LanguageDecision:
    if forced:
        return LanguageDecision([forced], False, f"forced via configuration ({forced})")

    py_score = 0
    java_score = 0

    py_score += min(len(facts.python_files), 50)
    java_score += min(len(facts.java_files), 50)

    dep_names = facts.manifest_dep_names()
    if dep_names & _PY_FRAMEWORK_DEPS:
        py_score += 40
    if any(hint in dep_names for hint in _JAVA_WEB_DEP_HINTS) or any(
        name.startswith(("org.springframework", "javax.ws.rs", "jakarta.ws.rs"))
        for name in dep_names
    ):
        java_score += 40
    if facts.annotation_hits:
        java_score += 20
    if facts.import_hits.keys() & {"fastapi", "flask", "django", "rest_framework"}:
        py_score += 20

    if py_score and java_score:
        # polyglot repo: analyze both, biggest signal first
        order = ["python", "java"] if py_score >= java_score else ["java", "python"]
        return LanguageDecision(order, False, f"polyglot (py={py_score}, java={java_score})")
    if py_score:
        return LanguageDecision(["python"], False, f"python signals (score={py_score})")
    if java_score:
        return LanguageDecision(["java"], False, f"java signals (score={java_score})")
    return LanguageDecision([], True, "no python or java source/manifest signals found")


def resolve_ambiguity_interactively(decision: LanguageDecision) -> LanguageDecision:
    """Ask the user once (spec requirement) when detection is ambiguous."""
    if not decision.ambiguous:
        return decision
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Could not detect project language automatically and no TTY is available. "
            "Pass --language python|java (and optionally --framework)."
        )
    import typer

    answer = typer.prompt("Is this project 'python' or 'java'?").strip().lower()
    if answer not in ("python", "java"):
        raise RuntimeError(f"Unsupported language answer: {answer!r}")
    log.info("language resolved interactively: %s", answer)
    return LanguageDecision([answer], False, "resolved interactively by user")
