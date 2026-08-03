# openapi-agent

**openapi-agent reads an existing Java or Python backend and produces an accurate
OpenAPI 3.1 specification for its HTTP API — without ever running the code.**

It analyzes your source the way a linter does (static analysis), discovers every
endpoint and the data it accepts and returns, and writes a standard `openapi.yaml`
that Swagger UI, Redoc, Postman, and code generators all understand. An optional LLM
polishes the human-readable descriptions, but it can never invent or change the actual
API contract.

---

## Table of contents

- [What it does](#what-it-does)
- [Installation](#installation)
- [Quick start](#quick-start)
- [End-to-end walkthrough (how it actually works)](#end-to-end-walkthrough-how-it-actually-works)
- [Design principles](#design-principles)
- [How Python and Java are handled](#how-python-and-java-are-handled)
- [Supported frameworks](#supported-frameworks)
- [CLI commands and flags](#cli-commands-and-flags)
- [Configuration](#configuration)
- [LLM providers](#llm-providers)
- [Viewing and serving the docs](#viewing-and-serving-the-docs)
- [The readiness report](#the-readiness-report)
- [Project layout](#project-layout)
- [Extending: add your own framework](#extending-add-your-own-framework)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development and testing](#development-and-testing)

---

## What it does

**Input:** a path to a repository (a FastAPI/Flask/Django service, a Spring or JAX-RS
project, a Maven multi-module build, or a multi-service monorepo).

**Output:** three files in `output/`:

| File | What it is |
|---|---|
| `api_metadata.json` | Intermediate facts extracted from the code (see [walkthrough](#end-to-end-walkthrough-how-it-actually-works)). Every fact carries *where it came from* and *how confident* the tool is. |
| `openapi.yaml` | The final OpenAPI 3.1 document — the deliverable. (One per service in a multi-service repo, plus a `*.catalog.json` index.) |
| `readiness_report.json` | Coverage, confidence, validation results, and a 0–100 readiness score. |

Concretely, a handler like this:

```python
@router.get("/pets/{pet_id}", response_model=Pet)
def get_pet(pet_id: int):
    return find_pet(pet_id)          # find_pet raises HTTPException(404) when missing
```

becomes this in `openapi.yaml`:

```yaml
/api/v1/pets/{pet_id}:
  get:
    operationId: store.store.routes.pets.get_pet.get
    summary: Fetch a single pet by its id
    parameters:
      - name: pet_id
        in: path
        required: true
        schema: { type: integer }
    responses:
      '200': { description: ..., content: { application/json: { schema: { $ref: '#/components/schemas/Pet' } } } }
      '404': { description: ..., content: { application/json: { schema: { type: object, properties: { detail: { type: string } } } } } }
```

Notice it picked up the `/api/v1` prefix, typed `pet_id` as an integer, linked the
`Pet` model, **and** discovered the `404` by following the call into `find_pet` — none
of which is written in the decorator.

---

## Installation

**Prerequisite:** Python 3.11 or newer. (A JDK + Maven are only needed for the optional
Java booster — see the end of this section.)

**1 — Get the code**

```bash
git clone https://github.com/<your-org>/api_spec_agent.git
cd api_spec_agent
```

**2 — Create an isolated virtual environment** (keeps these packages out of your global
Python). Do this once per clone:

```bash
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now show `(.venv)`. Re-run the activate command in every new terminal.

**3 — Install the tool and its dependencies into the venv**

```bash
pip install -e ".[dev]"
```

- `-e` (*editable*) means the code stays in `src/` and your edits take effect with no
  reinstall.
- `[dev]` also installs the LLM SDKs and `pytest`. For a runtime-only install, use
  `pip install -e .`.

**4 — Configure**

```bash
cp .env.example .env         # Windows: copy .env.example .env
```

Open `.env` and set `PROJECT_ROOT` (the repo you want to document) **and** a working
LLM provider + key (`LLM_PROVIDER` and, for the default, `GOOGLE_API_KEY`). Both are
required: the path is read only from `.env`/`config.yaml`, and generation aborts if no
usable LLM key is configured.

**Optional — Java precision booster.** For solver-grade Java type resolution (e.g.
unwrapping `ResponseEntity<Page<User>>`), build the bundled JVM sidecar — see
[`tools/java-sidecar/README.md`](tools/java-sidecar/README.md). It needs a JDK + Maven.
Without it, Java analysis still works via tree-sitter; some types are just marked
lower-confidence. This is expected and produces a `W401` warning, not an error.

---

## Quick start

The target repository and the LLM provider are configured **only in `.env`** —
there is no `--project-root` flag and no template-only mode. Set them once:

```env
# .env
PROJECT_ROOT=./path/to/service     # the repo you want documented
LLM_PROVIDER=gemini                # gemini | anthropic | openai
GOOGLE_API_KEY=<your-real-key>     # a usable key is REQUIRED — the run aborts without it
```

Then:

```bash
# Analyze the repo (from PROJECT_ROOT) and generate its spec:
python -m openapi_agent run

# View it in a local Swagger UI:
python -m openapi_agent serve --openapi-output ./output/openapi.yaml --port 8081
# open http://localhost:8081
```

To try it against the bundled example first, point `PROJECT_ROOT` at a fixture:

```env
PROJECT_ROOT=./tests/fixtures/fastapi_app
```
```bash
python -m openapi_agent run
```

---

## End-to-end walkthrough (how it actually works)

This is the whole pipeline, in order, following one command (the repo path and
LLM provider come from `.env`):

```bash
python -m openapi_agent run
```

The `run` command does **analyze → generate → validate → report**. Here is each step.

### Step 0 — Resolve settings

`cli.py` (built with **Typer**) collects your flags and merges all configuration
sources in this priority order:

```
CLI flags  >  config.yaml  >  .env  >  built-in defaults
```

The result is one validated `AgentConfig` object (a **pydantic** model) that the rest
of the program reads from. Nothing else touches environment variables directly.

### Step 1 — Scan the repository (detection)

`detection/repo.py` walks the repo **once**, cheaply, and builds a `RepoFacts` snapshot:

- It **skips** dependency and build folders (`.venv`, `node_modules`, `target/`,
  `build/`, `dist/`, `__pycache__`, `migrations`, dot-folders, …) and only notices
  `.py` and `.java` source files. Frontend files are ignored.
- It parses **manifests** (`pyproject.toml`, `requirements.txt`, `pom.xml`,
  `build.gradle`, `Dockerfile`, `docker-compose.yml`) to learn dependencies and module
  layout.
- It does a fast text scan of each source file for **signals** — Python imports
  (`fastapi`, `flask`, `django`, …) and Java annotations/imports (`@RestController`,
  `jakarta.ws.rs`, …).

No real parsing happens yet — this is just "what's in here and what does it smell like".

### Step 2 — Decide the language and pick adapters

`detection/language.py` scores Python vs Java from the facts (file counts + manifest
dependencies + import signals). If it's genuinely ambiguous and you're at a terminal,
it asks once; otherwise use `--language`.

Then `analysis/base.py` loads every **adapter** (one module per framework, registered as
plugins — see [Extending](#extending-add-your-own-framework)), keeps the ones matching
the detected language, and asks each `can_handle(facts)`. Each returns a 0–1 score;
those scoring ≥ 0.5 activate. So a FastAPI repo activates the FastAPI adapter, a Spring
repo the Spring adapter, and a mixed repo can activate several.

### Step 3 — Build the analysis toolchain

For the activated adapters, the tool builds a language-specific **context** it will read
from (heavy tools are created lazily, only when first needed):

- **Python** (`analysis/python/`): a **tree-sitter** index (fast, error-tolerant
  structure), plus **libcst** (exact syntax + docstrings), **astroid** (follows names and
  imports across files to resolve types), **griffe** (docstring text for descriptions),
  and **grimp** (import graph, to trace one function calling another).
- **Java** (`analysis/java/`): a **tree-sitter-java** index, plus — if a JDK is present —
  the **JavaParser sidecar** for deep type resolution. Both never execute your code.

### Step 4 — Extract the API (the heart of Phase 1)

Each adapter runs three methods in turn (`analysis/pipeline.py` orchestrates this):

1. **`discover_services`** — find the deployable app(s). For FastAPI that's each
   `FastAPI()` object; for Flask each `Flask()`; for Django the `settings.py` project;
   for Spring the controllers grouped by Maven/Gradle module. Each becomes a *Service*
   with its base path (e.g. `/api/v1` from a router prefix or `context-path`).

2. **`discover_routes`** — enumerate every route declaration: decorators
   (`@app.get`), `include_router`/blueprint graphs, Django `urls.py` include trees and
   DRF routers, Spring/JAX-RS annotations, and Spring WebFlux functional routers. Each
   becomes a lightweight *RouteRef* (URL + method + which function handles it).

3. **`extract_operation`** — for each route, work out the full contract:
   - **Parameters** from the handler signature — path (`{pet_id}` → integer),
     query, header, cookie.
   - **Request body** from the typed body parameter / serializer / DTO.
   - **Response shapes** from the return type / `response_model` / serializer.
   - **Error responses** by following the call chain: if the handler (or a service it
     calls) raises `HTTPException(404)`, throws a mapped exception, or hits a
     `@ControllerAdvice`/`ExceptionMapper`, that status is discovered even though it
     isn't in the decorator.
   - **Security** only when *proven* in source (a FastAPI `Security(...)` dependency,
     a DRF `authentication_classes`, a Spring `SecurityFilterChain`).

   Data models (Pydantic models, dataclasses, DRF serializers, Java DTOs) are converted
   to **JSON Schema** by a dedicated converter, which stores each model once in a
   **schema registry** and references it by `$ref` — so `Pet` is defined once and reused,
   and recursive models (`User → Team → User`) are handled safely.

   Every fact is recorded with **evidence** (file + line it came from) and a
   **confidence** level. If something can't be resolved, it becomes an open
   placeholder (`{}`) marked low-confidence — **never a guess** (see
   [Design principles](#design-principles)).

### Step 5 — Write the metadata file

All services, endpoints, and the shared schema registry are assembled, sorted into a
**deterministic** order (same code always yields byte-identical output), checked against
the tool's own published JSON Schema (`schemas/metadata-v1.schema.json`), and written
atomically to `output/api_metadata.json`.

This file is the **contract between the two phases**. You can inspect it, diff it, or
regenerate the spec from it without re-analyzing the code.

### Step 6 — Build the OpenAPI document (Phase 2 begins)

`openapi/generator.py` reads the metadata and, for each service:

- `openapi/components.py` turns the schema registry into OpenAPI
  `components.schemas`, giving each model a clean name, deduplicating identical shapes,
  and rewriting internal `$ref`s.
- `openapi/builder.py` assembles the document **mechanically**: `info`, `servers`,
  `paths`, operations, parameters, request bodies, and responses. When several code
  paths return the same status code with different shapes, it merges them into an
  `anyOf`. This step is pure — the same metadata always yields the same structure.

### Step 7 — Add descriptions (LLM, required)

The builder asks an **LLM enricher** for human-readable text (summaries, descriptions,
tags). An LLM is **required**: `run`/`generate` abort up front if no usable provider/key
is configured — there is no template-only CLI mode.

- The **LLM** (Gemini by default) is called **one endpoint at a time**. It receives only
  *compact metadata* — never your source code — and must return structured JSON. A
  **grounding checker** rejects any output that mentions a status code, parameter, path,
  or auth requirement not present in the metadata; responses are cached.
- For resilience, a *transient* per-call failure mid-run falls back to a deterministic
  **template** for that one operation (using your docstrings/code hints), so a single API
  hiccup never aborts a whole run. The document is still produced.

**The LLM only writes prose.** Paths, methods, fields, types, statuses, and security are
fixed by Phase 1 and never touched — so the structural document is identical with or
without the LLM.

### Step 8 — Serialize and validate

`openapi/writer.py` writes the document with **ruamel.yaml** (stable key order) to a temp
file, verifies it parses, then atomically replaces the target — a crash never leaves a
half-written file.

`openapi/validators.py` then runs a **validation ladder**:

1. Syntax round-trip.
2. **openapi-spec-validator** (OpenAPI 3.1) + **jsonschema** (each schema is valid JSON
   Schema 2020-12).
3. `$ref` resolution (an internal resolver, plus **prance**).
4. *Optional* Redocly / Spectral lint (skipped with a warning if Node isn't installed).
5. *Optional* schemathesis load check.

Then **programmatic gates** compare the document back against the Phase-1 metadata:
unique `operationId`s, every `{path param}` declared, valid status keys, **100% of
discovered endpoints present**, and **zero invention** (nothing in the document that
isn't backed by metadata). With `--strict`, a failed gate exits non-zero.

### Step 9 — Report

`reporting/report.py` writes `output/readiness_report.json` and prints a table:
endpoint/operation counts, request/response completeness, the high/medium/low confidence
split, unresolved contracts, validation results, and a **0–100 readiness score** per
service.

**That's the whole pipeline.** `analyze` runs Steps 1–5, `generate` runs Steps 6–8,
`run` does everything, and `validate`/`report`/`serve` operate on the results.

---

## Design principles

- **Never execute the target code.** Everything is static analysis, so it's safe to run
  against untrusted repos and needs no working environment for the target project.
- **Never guess.** Anything unresolved becomes a valid "anything goes" schema marked
  low-confidence, with the reason recorded — never a plausible-looking fabrication.
- **Deterministic.** The same commit always produces byte-identical output (enforced by a
  test). Diffs in your spec mean real changes in your code.
- **Evidence + confidence on every fact.** The metadata records the file/line each fact
  came from and how sure the tool is, so you can audit anything.
- **Two phases with a clean contract.** Facts (Phase 1) are separated from formatting
  (Phase 2). You can inspect, diff, or re-render from the metadata.
- **LLM is cosmetic and fenced in.** It only writes descriptions, only sees metadata, is
  grounding-checked, and always has a deterministic fallback.

---

## How Python and Java are handled

Both languages produce the **same intermediate metadata**, so Phase 2 is identical for
both. Only the *reading* differs:

| | Python | Java |
|---|---|---|
| Structure scan | tree-sitter-python | tree-sitter-java |
| Deep reading | libcst + astroid | tree-sitter facts + optional JavaParser sidecar |
| Docstrings / graph | griffe + grimp | Javadoc + import resolution |
| External tools needed | none | none required; JDK+Maven only for the optional booster |
| Degrades gracefully | untyped handlers → best-effort/low-confidence | no sidecar → tree-sitter only, marked lower-confidence |

A **mixed repository** (e.g. a Python API plus a Java service) is fine: each adapter
handles its own files, and each service gets its own spec.

---

## Supported frameworks

| Language | Framework | Adapter name | Highlights |
|---|---|---|---|
| Python | FastAPI | `fastapi` | router/prefix graphs, Pydantic v1/v2 constraints, `Depends`/`Security` schemes + scopes, `HTTPException` call-chain analysis, file/stream/redirect responses |
| Python | Flask | `flask` | blueprints, `MethodView`, path converters, `request.*` usage inference, `abort()`/werkzeug errors, `errorhandler` |
| Python | Django / DRF | `django` | `urls.py` include tree, path converters, ViewSets/routers/`@action`, serializers (fields/constraints/`read_only`/nested), auth & permission classes |
| Java | Spring MVC | `spring-mvc` | `@RequestMapping` composition, `ResponseEntity<T>` & generics, Jackson + Bean Validation, `@ControllerAdvice`, `@PreAuthorize` + `SecurityFilterChain`, multipart |
| Java | Spring WebFlux | `spring-webflux` | annotated `Mono`/`Flux` routes and functional `RouterFunction` beans |
| Java | JAX-RS / Jakarta REST | `jaxrs` | `@Path` composition, `@Produces`/`@Consumes`, all param annotations, `ExceptionMapper`, `@ApplicationPath`, `@RolesAllowed` |

Monoliths, Maven/Gradle multi-module builds, and multi-service (docker-compose) repos are
all supported. Multi-service repos emit **one spec per service** plus a
`*.catalog.json` index.

---

## CLI commands and flags

```bash
python -m openapi_agent analyze     # Phase 1 only  → output/api_metadata.json
python -m openapi_agent generate    # Phase 2 only  → output/openapi.yaml (needs metadata)
python -m openapi_agent run         # analyze + generate + validate + report
python -m openapi_agent validate    # validate an existing OpenAPI document
python -m openapi_agent report      # re-print the readiness report
python -m openapi_agent serve       # local Swagger UI for the generated spec
```

Common flags:

```bash
python -m openapi_agent run \
  --language python \                 # force language (skip auto-detect)
  --framework fastapi \               # force adapter(s)
  --service billing \                 # limit to one service (repeatable) in multi-service repos
  --provider gemini --model gemini-2.5-pro \   # LLM provider + model (key still from .env)
  --metadata-output out/meta.json \
  --openapi-output out/openapi.yaml \
  --format yaml \                     # yaml | json
  --strict \                          # fail (exit 2) if a gate fails
  --verbose                           # debug logging
```

> **The repository path has no CLI flag** — set `PROJECT_ROOT` in `.env` (or
> `config.yaml`). Likewise there is **no `--no-llm` flag**: an LLM is always required and
> `run`/`generate` exit with code 2 if no usable provider/key is configured.

Precedence is always **CLI > config.yaml > .env > defaults** (the repo path and LLM key
have no CLI layer, so for them it is **config.yaml > .env > defaults**).

---

## Configuration

Three layers, highest priority first:

1. **CLI flags** — for one-off overrides.
2. **`config.yaml`** — copy `config.example.yaml`. Richer settings: extra `servers`,
   custom `exclude_dirs`, optional validation layers (Redocly/Spectral/schemathesis),
   the Java sidecar path/timeout, and reserved booster flags.
3. **`.env`** — copy `.env.example`. Paths, LLM provider + API keys, and OpenAPI
   `info`/`server` defaults. **`.env` is git-ignored and API keys are never logged or
   written to output.**

Key `.env` values:

```env
PROJECT_ROOT=./target-repo   # REQUIRED — the only place the repo path is set
OUTPUT_PATH=./output/openapi.yaml
METADATA_PATH=./output/api_metadata.json
LLM_PROVIDER=gemini          # gemini | anthropic | openai (required; 'none' is rejected by the CLI)
GOOGLE_API_KEY=...           # REQUIRED for the chosen provider — the run aborts without a usable key
OPENAPI_SERVER_URL=http://localhost:8080/api/v1
STRICT_MODE=false
OUTPUT_FORMAT=yaml
```

---

## LLM providers

| `.env` setting | Meaning |
|---|---|
| `LLM_PROVIDER` | `gemini` (default) · `anthropic` · `openai` (required; `none` is for internal/test use and is rejected by the CLI) |
| `GOOGLE_API_KEY` / `GOOGLE_MODEL` | Gemini (`gemini-2.5-flash` default; `gemini-2.5-pro` for best quality) |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` · `OPENAI_API_KEY` / `OPENAI_MODEL` | alternates |
| `LLM_TIMEOUT_SECONDS` · `LLM_MAX_RETRIES` · `LLM_CACHE_DIR` | runtime controls; responses cached by metadata hash |

The LLM is used **only for descriptive text**, receives **only compact metadata (never
source)**, must return schema-valid JSON, and is **grounding-checked**. It is
**required**: configure a provider and a usable key in `.env`, or `run`/`generate` abort
before doing any work. A *transient* per-call failure during a run still falls back to a
deterministic template for that one operation, so a single hiccup never aborts the run —
but a missing/placeholder key is treated as a configuration error, not a fallback.

---

## Viewing and serving the docs

**Option 1 — Built-in (recommended).** Bundles Swagger UI (via the `swagger-ui-bundle`
package, no internet needed) and serves your spec:

```bash
python -m openapi_agent serve --openapi-output ./output/openapi.yaml --port 8081
# → http://localhost:8081
```

**Option 2 — Swagger UI via Docker:**

```bash
docker run -p 8081:8080 -e SWAGGER_JSON=/spec/openapi.yaml -v $(pwd)/output:/spec swaggerapi/swagger-ui
# → http://localhost:8081
```

**Option 3 — Redoc (requires Node):**

```bash
npx @redocly/cli preview-docs ./output/openapi.yaml --port 8081
```

**Option 4 — Static HTML export:**

```bash
npx @redocly/cli build-docs ./output/openapi.yaml -o ./output/docs.html
```

**Option 5 — Postman:** *Import* → select `output/openapi.yaml` → a full collection is
created using `OPENAPI_SERVER_URL` as the base URL.

> **"Try it out" note:** interactive requests go to `OPENAPI_SERVER_URL` (default
> `http://localhost:8080/api/v1`). The target API must be **running** and
> **CORS-enabled** for in-browser calls to work — the docs describe the API, they don't
> host it.

---

## The readiness report

`output/readiness_report.json` (rendered as a table by the `report` command) tells you
how trustworthy the output is:

- Endpoint and operation counts.
- Request / response / parameter **completeness** (% with resolved schemas).
- **Confidence distribution** — high / medium / low across all facts.
- **Unresolved contracts** — the exact spots that fell back to `{}`, with reasons.
- Validation errors/warnings and per-gate pass/fail.
- A **0–100 readiness score** per service.

A low score points you straight at the code the tool couldn't fully understand (usually
untyped handlers or, for Java, missing sidecar type resolution).

---

## Project layout

```
api_spec_agent/
├── src/openapi_agent/
│   ├── cli.py                 # Typer CLI (analyze/generate/run/validate/report/serve)
│   ├── config/                # settings (.env) + config.yaml merge + precedence
│   ├── detection/             # repo scan, manifest parsing, language decision
│   ├── models/                # metadata data model + schema registry (pydantic)
│   ├── analysis/
│   │   ├── base.py            # FrameworkAdapter interface + plugin loader
│   │   ├── pipeline.py        # Phase-1 orchestrator → api_metadata.json
│   │   ├── python/            # tree-sitter/libcst/astroid/griffe/grimp + adapters
│   │   │   └── adapters/      # fastapi.py, flask.py, django.py
│   │   └── java/              # tree-sitter-java + sidecar client + adapters
│   │       └── adapters/      # spring.py, jaxrs.py
│   ├── llm/                   # provider adapters + grounding + cache + templates
│   ├── openapi/               # builder, components, writer, validators, generator
│   ├── reporting/             # readiness report
│   └── serve/                 # bundled Swagger UI server
├── tools/java-sidecar/        # optional JavaParser JVM sidecar (source + build)
├── schemas/                   # published Phase-1 metadata JSON Schema
├── tests/fixtures/            # one example repo per framework
└── config.example.yaml, .env.example
```

---

## Extending: add your own framework

Adapters are plugins — the core needs no changes.

1. Implement `openapi_agent.analysis.base.FrameworkAdapter`:
   `can_handle(RepoFacts) -> DetectionResult` (pure scoring, no parsing),
   `discover_services`, `discover_routes`, and `extract_operation`. Emit the neutral
   models from `openapi_agent.models` with `Evidence` + `Confidence` on every fact, and
   degrade to `{}` + low confidence instead of raising.
2. Register it (in your own package) under the entry-point group:

   ```toml
   [project.entry-points."openapi_agent.adapters"]
   litestar = "my_pkg.litestar_adapter:LitestarAdapter"
   ```

3. `pip install` your package — it's discovered and activated automatically.

Study `analysis/python/adapters/fastapi.py` or `analysis/java/adapters/spring.py` as
references.

---

## Limitations

- **Static analysis only.** Routes registered via reflection, runtime loops, or
  config-driven dispatch are reported as warnings, not guessed.
- Python handlers without type annotations get best-effort schemas from literal returns;
  otherwise `{}` low-confidence (honest, shown in the report).
- Without the Java sidecar, only types defined inside the repo are resolved; external
  library DTOs become `{}` with `sidecar_unavailable` confidence.
- WebFlux functional-route bodies are inherently dynamic — inferred from
  `body(..., X.class)` hints at medium confidence.
- Security is documented **only when proven**; `@PreAuthorize`/`@RolesAllowed` without a
  provable scheme produce a warning, not a guess.
- The `use_pyright` / `use_mypy` / `scip_index` config options are reserved for future
  inference boosters; enabling them today emits warning `W006` and does nothing else.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no framework adapter activated` | Framework not detected in manifests/imports. Force with `--language` + `--framework`. |
| `W401 JVM sidecar unavailable` | Expected until you build `tools/java-sidecar` with a JDK on PATH. Analysis still works at reduced confidence. |
| Prompted for the language | Detection was ambiguous and there's no terminal; pass `--language python\|java`. |
| `lint skipped (Node tooling not found)` | Redocly/Spectral need `npx`; install Node or leave those layers off in `config.yaml`. |
| `LLM is required ...` / exit code 2 before any analysis | No usable provider/key in `.env`. Set `LLM_PROVIDER` (not `none`) and the matching key (e.g. `GOOGLE_API_KEY`, not the `your-...` placeholder). |
| Descriptions look generic / templated | A *transient* provider failure mid-run fell back to templates for some operations (check log warnings — keys are redacted). Re-run once the provider is reachable. |
| Empty document for a service | See `output/readiness_report.json` warnings (`W2xx` route resolution) and re-run with `--verbose`. |
| Strict run exits with code 2 | A gate failed; the report names which (coverage, invention, or high-confidence alteration). |

---

## Development and testing

```bash
pytest -q                 # full unit + integration suite over the fixture repos
pytest -q -k fastapi      # just one framework
```

Fixture repos live in `tests/fixtures/` — one per supported framework, plus
multi-module and microservices examples. The Phase-1 metadata schema at
`schemas/metadata-v1.schema.json` is generated from the pydantic models, so the emitted
metadata and the published schema can never drift apart.
