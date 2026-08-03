package dev.openapiagent.sidecar;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/**
 * openapi-agent JVM sidecar: authoritative symbol/type resolution facts.
 *
 * Usage: java -jar openapi-agent-sidecar.jar --repo &lt;path&gt; --format json
 *
 * Emits versioned JSON on stdout (sidecar_facts_version 1.0.0):
 * {"sidecar_facts_version":"1.0.0","types":[
 *   {"qualifiedName":"com.acme.Dto","file":"src/main/java/...",
 *    "fields":[{"name":"items","resolvedType":"java.util.List&lt;com.acme.Item&gt;"}],
 *    "methods":[{"name":"find","resolvedReturnType":"com.acme.Item",
 *                "parameters":[{"name":"id","resolvedType":"long"}]}]}]}
 *
 * The sidecar only ever READS sources; it never compiles or executes them.
 */
public final class Main {

    static final String FACTS_VERSION = "1.0.0";

    public static void main(String[] args) throws IOException {
        Path repo = null;
        for (int i = 0; i < args.length - 1; i++) {
            if ("--repo".equals(args[i])) {
                repo = Paths.get(args[i + 1]).toAbsolutePath().normalize();
            }
        }
        if (repo == null || !Files.isDirectory(repo)) {
            System.err.println("usage: --repo <path> [--format json]");
            System.exit(2);
            return;
        }

        CombinedTypeSolver solver = new CombinedTypeSolver();
        solver.add(new ReflectionTypeSolver());
        List<Path> sourceRoots = findSourceRoots(repo);
        for (Path root : sourceRoots) {
            solver.add(new JavaParserTypeSolver(root));
        }

        ParserConfiguration configuration = new ParserConfiguration()
                .setSymbolResolver(new JavaSymbolSolver(solver))
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE);
        JavaParser parser = new JavaParser(configuration);

        List<Map<String, Object>> types = new ArrayList<>();
        try (Stream<Path> paths = Files.walk(repo)) {
            paths.filter(p -> p.toString().endsWith(".java"))
                    .filter(p -> !isExcluded(repo.relativize(p)))
                    .sorted()
                    .forEach(p -> parseFile(parser, repo, p, types));
        }

        Map<String, Object> document = new LinkedHashMap<>();
        document.put("sidecar_facts_version", FACTS_VERSION);
        document.put("types", types);
        Gson gson = new GsonBuilder().disableHtmlEscaping().create();
        System.out.println(gson.toJson(document));
    }

    private static boolean isExcluded(Path relative) {
        for (Path part : relative) {
            String name = part.toString();
            if (name.equals("target") || name.equals("build") || name.equals("node_modules")
                    || name.startsWith(".")) {
                return true;
            }
        }
        return false;
    }

    private static List<Path> findSourceRoots(Path repo) throws IOException {
        List<Path> roots = new ArrayList<>();
        try (Stream<Path> paths = Files.walk(repo)) {
            paths.filter(Files::isDirectory)
                    .filter(p -> p.endsWith(Paths.get("src", "main", "java")))
                    .forEach(roots::add);
        }
        if (roots.isEmpty()) {
            roots.add(repo);
        }
        return roots;
    }

    private static void parseFile(JavaParser parser, Path repo, Path file, List<Map<String, Object>> out) {
        try {
            CompilationUnit unit = parser.parse(file).getResult().orElse(null);
            if (unit == null) {
                return;
            }
            String relative = repo.relativize(file).toString().replace('\\', '/');
            unit.findAll(TypeDeclaration.class).forEach(type -> {
                Map<String, Object> typeFacts = new LinkedHashMap<>();
                typeFacts.put("qualifiedName",
                        type.getFullyQualifiedName().orElse(type.getNameAsString()).toString());
                typeFacts.put("file", relative);

                List<Map<String, Object>> fields = new ArrayList<>();
                for (Object member : type.getFields()) {
                    FieldDeclaration field = (FieldDeclaration) member;
                    field.getVariables().forEach(variable -> {
                        Map<String, Object> fieldFacts = new LinkedHashMap<>();
                        fieldFacts.put("name", variable.getNameAsString());
                        fieldFacts.put("resolvedType", resolveOr(variable.getType()::resolve,
                                variable.getTypeAsString()));
                        fields.add(fieldFacts);
                    });
                }
                typeFacts.put("fields", fields);

                List<Map<String, Object>> methods = new ArrayList<>();
                type.getMethods().forEach(m -> {
                    MethodDeclaration method = (MethodDeclaration) m;
                    Map<String, Object> methodFacts = new LinkedHashMap<>();
                    methodFacts.put("name", method.getNameAsString());
                    methodFacts.put("resolvedReturnType", resolveOr(method.getType()::resolve,
                            method.getTypeAsString()));
                    List<Map<String, Object>> parameters = new ArrayList<>();
                    method.getParameters().forEach(parameter -> {
                        Map<String, Object> parameterFacts = new LinkedHashMap<>();
                        parameterFacts.put("name", parameter.getNameAsString());
                        parameterFacts.put("resolvedType", resolveOr(parameter.getType()::resolve,
                                parameter.getTypeAsString()));
                        parameters.add(parameterFacts);
                    });
                    methodFacts.put("parameters", parameters);
                    methods.add(methodFacts);
                });
                typeFacts.put("methods", methods);
                out.add(typeFacts);
            });
        } catch (Exception e) {
            // one broken file must not sink the run; the Python side treats
            // missing facts as reduced confidence
            System.err.println("WARN " + file + ": " + e.getMessage());
        }
    }

    private interface Resolver {
        Object resolve() throws Exception;
    }

    private static String resolveOr(Resolver resolver, String fallback) {
        try {
            Object resolved = resolver.resolve();
            return resolved.toString().replace("? extends ", "").replace("? super ", "");
        } catch (Exception e) {
            return fallback;
        }
    }
}
