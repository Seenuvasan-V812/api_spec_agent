# openapi-agent JVM sidecar

Authoritative Java symbol/type resolution for openapi-agent, built on
**JavaParser + JavaSymbolSolver** (optionally **Spoon** via `-Pspoon`).

The sidecar is an *optional precision booster*. Without it, openapi-agent
falls back to tree-sitter-only Java extraction and marks affected contracts
as reduced confidence (`sidecar_unavailable`) — the pipeline still succeeds.

## Build

Requires JDK 17+ and Maven 3.8+:

```bash
cd tools/java-sidecar
mvn -q package            # -> target/openapi-agent-sidecar.jar (fat JAR)
# optional Spoon backend:
mvn -q package -Pspoon
```

No Maven installed? Generate the wrapper once from any machine with Maven
(`mvn wrapper:wrapper`) and commit it, or install Maven via your package
manager (`winget install Apache.Maven`, `brew install maven`, `apt install maven`).

## Use

openapi-agent invokes it automatically when the JAR exists at the configured
path (`analysis.java.sidecar_jar` in `config.yaml`, default
`tools/java-sidecar/target/openapi-agent-sidecar.jar`) and a `java`
executable is on PATH. Manual invocation:

```bash
java -jar target/openapi-agent-sidecar.jar --repo /path/to/java/project --format json
```

Output: versioned JSON on stdout (`sidecar_facts_version` 1.x). The Python
client (`openapi_agent/analysis/java/sidecar_client.py`) hard-fails on a
major version mismatch rather than mis-parsing facts.

The sidecar only reads sources; it never compiles, builds, or executes the
target project.
