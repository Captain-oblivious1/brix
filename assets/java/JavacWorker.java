// A persistent javac worker for brix (see src/brix/workers.py).
//
// Reads the brix worker protocol (one JSON object per line) on stdin and
// compiles in-process via javax.tools.JavaCompiler — no per-request JVM
// launch, no re-exec of javac. The JVM stays warm across requests.
//
// Request  : {"id": N, "args": [...], "cwd": "/abs"}
// Response : {"id": N, "exitCode": C, "stdout": "...", "stderr": "..."}
//
// Argument convention (produced by brix.java.JarAction; multi-value flags,
// since brix renders collections space-separated):
//   --sources   <file>...     source files to compile
//   --classpath <jar>...       joined with File.pathSeparator onto -classpath
//   --processorpath <jar>...  joined with File.pathSeparator onto -processorpath
//   --options   <opt>...       extra javac options, passed through
//   -d          <dir>          output directory
// Relative paths are resolved against the request's cwd, so the worker is
// stateless between requests (a future cross-build daemon can serve many
// workspaces). stdout is reserved for the protocol; compiler output travels
// in the response fields.
//
// Dependency-free on purpose: it is bootstrap-compiled by a plain javac node
// and must not need anything but the JDK.

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

public class JavacWorker {

    public static void main(String[] argv) throws Exception {
        BufferedReader in =
            new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        PrintStream out =
            new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
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
            Result r = compile(args, cwd);
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

    static Result compile(List<String> tokens, String cwd) {
        List<String> sources = new ArrayList<>();
        List<String> classpath = new ArrayList<>();
        List<String> processorpath = new ArrayList<>();
        List<String> options = new ArrayList<>();
        String outDir = null;
        String mode = "sources";
        for (int i = 0; i < tokens.size(); i++) {
            String t = tokens.get(i);
            if (t.equals("--sources")) {
                mode = "sources";
            } else if (t.equals("--classpath")) {
                mode = "classpath";
            } else if (t.equals("--processorpath")) {
                mode = "processorpath";
            } else if (t.equals("--options")) {
                mode = "options";
            } else if (t.equals("-d")) {
                if (i + 1 < tokens.size()) {
                    outDir = resolve(cwd, tokens.get(++i));
                }
            } else if (mode.equals("classpath")) {
                classpath.add(resolve(cwd, t));
            } else if (mode.equals("processorpath")) {
                processorpath.add(resolve(cwd, t));
            } else if (mode.equals("options")) {
                options.add(t);
            } else {
                sources.add(resolve(cwd, t));
            }
        }

        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            return new Result(2, "", "brix: no system Java compiler (a JDK, not a JRE, is required)");
        }
        List<String> javacArgs = new ArrayList<>(options);
        if (!classpath.isEmpty()) {
            javacArgs.add("-classpath");
            javacArgs.add(String.join(File.pathSeparator, classpath));
        }
        if (!processorpath.isEmpty()) {
            javacArgs.add("-processorpath");
            javacArgs.add(String.join(File.pathSeparator, processorpath));
        }
        if (outDir != null) {
            javacArgs.add("-d");
            javacArgs.add(outDir);
        }
        javacArgs.addAll(sources);

        ByteArrayOutputStream outBytes = new ByteArrayOutputStream();
        ByteArrayOutputStream errBytes = new ByteArrayOutputStream();
        int code = compiler.run(null, outBytes, errBytes, javacArgs.toArray(new String[0]));
        return new Result(
            code,
            new String(outBytes.toByteArray(), StandardCharsets.UTF_8),
            new String(errBytes.toByteArray(), StandardCharsets.UTF_8));
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
