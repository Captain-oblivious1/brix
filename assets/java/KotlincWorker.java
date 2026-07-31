// A persistent kotlinc worker for brix (see src/brix/workers.py).
//
// Reads the brix worker protocol (one JSON object per line) on stdin and
// compiles in-process via the Kotlin CLI compiler — no per-request JVM
// launch, no re-exec of kotlinc. The JVM stays warm across requests, which
// matters far more than for javac: a cold kotlinc pays JVM startup *plus*
// loading and JIT-warming the Kotlin compiler on every invocation.
//
// Request  : {"id": N, "args": [...], "cwd": "/abs"}
// Response : {"id": N, "exitCode": C, "stdout": "...", "stderr": "..."}
//
// Argument convention (produced by brix.kotlin.JarAction; multi-value flags,
// since brix renders collections space-separated):
//   --sources        <file>...  source files to compile
//   --classpath      <jar>...    joined with File.pathSeparator onto -classpath
//   --plugin         <jar>...    each becomes -Xplugin=<abs> (compiler plugin jar)
//   --plugin-option  <opt>...   each becomes a -P <opt> (e.g. the all-open preset)
//   --options        <opt>...   extra kotlinc options, passed through (e.g.
//                               -jvm-target 17)
//   -d               <dir>       output directory
// Path-bearing args (--sources, --classpath, --plugin, -d) are resolved to
// absolute paths against the request's cwd, so the worker is stateless
// between requests (a cross-build daemon can serve many workspaces). Tokens
// in --options and --plugin-option are passed through verbatim, so any path
// embedded there resolves like a spawned kotlinc would (against the launch
// directory); brix keeps such paths off --options for that reason. stdout is
// reserved for the protocol; compiler diagnostics travel in the response.
//
// The Kotlin compiler is invoked by reflection into
// org.jetbrains.kotlin.cli.jvm.K2JVMCompiler, so this file compiles with a
// plain javac node (no Kotlin classes on the compile classpath, exactly like
// JavacWorker); the compiler is supplied on the *launch* classpath
// (kotlin-compiler.jar). K2JVMCompiler.exec(PrintStream, String...) returns
// an ExitCode and does not call System.exit, so it is safe to call in a loop.

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class KotlincWorker {

    public static void main(String[] argv) throws Exception {
        BufferedReader in =
            new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        PrintStream out =
            new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");

        Compiler compiler = new Compiler();  // one instance, reused warm

        String line;
        while ((line = in.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) {
                continue;
            }
            Map<String, Object> req = asObject(Json.parse(line));
            long id = ((Number) req.get("id")).longValue();
            String cwd = (String) req.get("cwd");
            List<String> args = new ArrayList<>();
            for (Object a : asArray(req.get("args"))) {
                args.add((String) a);
            }
            Result r = compiler.compile(args, cwd);
            StringBuilder sb = new StringBuilder();
            sb.append("{\"id\":").append(id)
              .append(",\"exitCode\":").append(r.code)
              .append(",\"stdout\":").append(Json.string(r.out))
              .append(",\"stderr\":").append(Json.string(r.err))
              .append("}");
            out.println(sb.toString());
            out.flush();
        }
    }

    static final class Result {
        final int code;
        final String out;
        final String err;
        Result(int code, String out, String err) {
            this.code = code;
            this.out = out;
            this.err = err;
        }
    }

    // Holds the reflected K2JVMCompiler instance and exec method, resolved
    // once. If the Kotlin compiler is not on the launch classpath, every
    // request fails loudly (exit 2, brix: message) — never silently.
    static final class Compiler {
        private final Object instance;
        private final Method exec;
        private final Method getCode;
        private final String initError;

        Compiler() {
            Object instance = null;
            Method exec = null;
            Method getCode = null;
            String initError = null;
            try {
                Class<?> k2 = Class.forName("org.jetbrains.kotlin.cli.jvm.K2JVMCompiler");
                instance = k2.getDeclaredConstructor().newInstance();
                exec = k2.getMethod("exec", PrintStream.class, String[].class);
                Class<?> exitCode = Class.forName("org.jetbrains.kotlin.cli.common.ExitCode");
                getCode = exitCode.getMethod("getCode");
            } catch (Throwable t) {
                initError = "brix: Kotlin compiler unavailable on the worker classpath "
                    + "(need kotlin-compiler.jar): " + t;
            }
            this.instance = instance;
            this.exec = exec;
            this.getCode = getCode;
            this.initError = initError;
        }

        Result compile(List<String> tokens, String cwd) {
            if (initError != null) {
                return new Result(2, "", initError);
            }
            List<String> sources = new ArrayList<>();
            List<String> classpath = new ArrayList<>();
            List<String> plugins = new ArrayList<>();
            List<String> pluginOptions = new ArrayList<>();
            List<String> options = new ArrayList<>();
            String outDir = null;
            String mode = "sources";
            for (int i = 0; i < tokens.size(); i++) {
                String t = tokens.get(i);
                if (t.equals("--sources")) {
                    mode = "sources";
                } else if (t.equals("--classpath")) {
                    mode = "classpath";
                } else if (t.equals("--plugin")) {
                    mode = "plugin";
                } else if (t.equals("--plugin-option")) {
                    mode = "plugin-option";
                } else if (t.equals("--options")) {
                    mode = "options";
                } else if (t.equals("-d")) {
                    if (i + 1 < tokens.size()) {
                        outDir = resolve(cwd, tokens.get(++i));
                    }
                } else if (mode.equals("classpath")) {
                    classpath.add(resolve(cwd, t));
                } else if (mode.equals("plugin")) {
                    plugins.add(resolve(cwd, t));
                } else if (mode.equals("plugin-option")) {
                    pluginOptions.add(t);
                } else if (mode.equals("options")) {
                    options.add(t);
                } else {
                    sources.add(resolve(cwd, t));
                }
            }

            List<String> kotlincArgs = new ArrayList<>(options);
            if (!classpath.isEmpty()) {
                kotlincArgs.add("-classpath");
                kotlincArgs.add(String.join(File.pathSeparator, classpath));
            }
            for (String plugin : plugins) {
                kotlincArgs.add("-Xplugin=" + plugin);
            }
            for (String option : pluginOptions) {
                kotlincArgs.add("-P");
                kotlincArgs.add(option);
            }
            if (outDir != null) {
                kotlincArgs.add("-d");
                kotlincArgs.add(outDir);
            }
            kotlincArgs.addAll(sources);

            // The compiler writes diagnostics to the PrintStream we hand exec();
            // some paths also touch System.out/err, so redirect both for the
            // duration to keep the protocol stdout clean, then restore.
            ByteArrayOutputStream diag = new ByteArrayOutputStream();
            PrintStream diagStream;
            try {
                diagStream = new PrintStream(diag, true, "UTF-8");
            } catch (Exception e) {
                diagStream = new PrintStream(diag, true);
            }
            PrintStream savedOut = System.out;
            PrintStream savedErr = System.err;
            System.setOut(diagStream);
            System.setErr(diagStream);
            int code;
            try {
                Object exitCode = exec.invoke(instance, diagStream, kotlincArgs.toArray(new String[0]));
                code = ((Number) getCode.invoke(exitCode)).intValue();
            } catch (Throwable t) {
                // A compiler-internal failure is not the action's deterministic
                // answer; report it as a nonzero exit with the detail.
                code = 2;
                t.printStackTrace(diagStream);
            } finally {
                System.setOut(savedOut);
                System.setErr(savedErr);
                diagStream.flush();
            }
            String text = new String(diag.toByteArray(), StandardCharsets.UTF_8);
            return new Result(code, "", text);
        }
    }

    static String resolve(String cwd, String path) {
        File f = new File(path);
        return f.isAbsolute() ? path : new File(cwd, path).getPath();
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> asObject(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    static List<Object> asArray(Object o) {
        return (List<Object>) o;
    }

    // --- minimal JSON (the JDK ships none) ---------------------------------

    static final class Json {
        static Object parse(String s) {
            return new Parser(s).value();
        }

        static String string(String v) {
            StringBuilder b = new StringBuilder("\"");
            for (int k = 0; k < v.length(); k++) {
                char c = v.charAt(k);
                switch (c) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    case '\b': b.append("\\b"); break;
                    case '\f': b.append("\\f"); break;
                    default:
                        if (c < 0x20) {
                            b.append(String.format("\\u%04x", (int) c));
                        } else {
                            b.append(c);
                        }
                }
            }
            return b.append('"').toString();
        }

        static final class Parser {
            final String s;
            int i;

            Parser(String s) {
                this.s = s;
            }

            Object value() {
                skip();
                char c = s.charAt(i);
                if (c == '{') return object();
                if (c == '[') return array();
                if (c == '"') return str();
                if (c == 't') { i += 4; return Boolean.TRUE; }
                if (c == 'f') { i += 5; return Boolean.FALSE; }
                if (c == 'n') { i += 4; return null; }
                return number();
            }

            Map<String, Object> object() {
                Map<String, Object> m = new LinkedHashMap<>();
                i++; // {
                skip();
                if (s.charAt(i) == '}') { i++; return m; }
                while (true) {
                    skip();
                    String key = str();
                    skip();
                    i++; // :
                    m.put(key, value());
                    skip();
                    char c = s.charAt(i++);
                    if (c == '}') break;
                }
                return m;
            }

            List<Object> array() {
                List<Object> l = new ArrayList<>();
                i++; // [
                skip();
                if (s.charAt(i) == ']') { i++; return l; }
                while (true) {
                    l.add(value());
                    skip();
                    char c = s.charAt(i++);
                    if (c == ']') break;
                }
                return l;
            }

            String str() {
                StringBuilder b = new StringBuilder();
                i++; // opening quote
                while (true) {
                    char c = s.charAt(i++);
                    if (c == '"') break;
                    if (c == '\\') {
                        char e = s.charAt(i++);
                        switch (e) {
                            case '"': b.append('"'); break;
                            case '\\': b.append('\\'); break;
                            case '/': b.append('/'); break;
                            case 'b': b.append('\b'); break;
                            case 'f': b.append('\f'); break;
                            case 'n': b.append('\n'); break;
                            case 'r': b.append('\r'); break;
                            case 't': b.append('\t'); break;
                            case 'u':
                                b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                                i += 4;
                                break;
                            default: b.append(e);
                        }
                    } else {
                        b.append(c);
                    }
                }
                return b.toString();
            }

            Object number() {
                int start = i;
                while (i < s.length() && "-+0123456789.eE".indexOf(s.charAt(i)) >= 0) {
                    i++;
                }
                String n = s.substring(start, i);
                if (n.indexOf('.') < 0 && n.indexOf('e') < 0 && n.indexOf('E') < 0) {
                    return Long.parseLong(n);
                }
                return Double.parseDouble(n);
            }

            void skip() {
                while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                    i++;
                }
            }
        }
    }
}
