// Repackages a plain application jar into a Spring Boot executable jar for
// brix (see src/brix/springboot.py).
//
// This does not reimplement the BOOT-INF layout: it drives Spring's own
// org.springframework.boot.loader.tools.Repackager — the same class the
// Maven/Gradle plugins call — so the output is byte-for-byte a standard
// Spring Boot jar runnable with `java -jar`.
//
// Argument convention (multi-value flags, since brix renders data
// collections space-separated). Relative paths resolve against cwd:
//   --source <jar>        the plain jar to repackage (BOOT-INF/classes payload)
//   --dest   <jar>        the executable jar to write
//   --main   <class>      the application main class (becomes Start-Class)
//   --tools  <jar>...     the spring-boot-loader-tools runtime classpath
//   --libs   <jar>...     the app's runtime dependency jars (-> BOOT-INF/lib)
//
// Dependency-free at compile time on purpose: loader-tools is loaded at
// runtime from the --tools jars through a child URLClassLoader and driven by
// reflection, so this helper is bootstrap-compiled by a plain javac node that
// needs nothing but the JDK. Passing every jar as a data argument (never a
// baked -classpath) keeps the brix command template free of machine-specific
// paths, so its fingerprint stays portable across machines and caches.

import java.io.File;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.net.URL;
import java.net.URLClassLoader;
import java.util.ArrayList;
import java.util.List;

public class BootRepackager {

    public static void main(String[] args) throws Exception {
        String source = null, dest = null, main = null;
        List<String> tools = new ArrayList<>();
        List<String> libs = new ArrayList<>();
        List<String> mode = null;
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--source": source = args[++i]; mode = null; break;
                case "--dest":   dest = args[++i];   mode = null; break;
                case "--main":   main = args[++i];   mode = null; break;
                case "--tools":  mode = tools; break;
                case "--libs":   mode = libs;  break;
                default:
                    if (mode == null) {
                        throw new IllegalArgumentException("unexpected argument: " + a);
                    }
                    mode.add(a);
            }
        }
        if (source == null || dest == null || main == null) {
            throw new IllegalArgumentException(
                "BootRepackager: --source, --dest and --main are required");
        }

        URL[] urls = new URL[tools.size()];
        for (int i = 0; i < tools.size(); i++) {
            urls[i] = new File(tools.get(i)).toURI().toURL();
        }
        // Child loader for loader-tools; parent is this helper's loader so the
        // dynamic Libraries proxy (defined against a child interface) resolves.
        URLClassLoader cl =
            new URLClassLoader(urls, BootRepackager.class.getClassLoader());

        Class<?> repackagerC = cl.loadClass("org.springframework.boot.loader.tools.Repackager");
        Class<?> librariesC = cl.loadClass("org.springframework.boot.loader.tools.Libraries");
        Class<?> libraryC = cl.loadClass("org.springframework.boot.loader.tools.Library");
        Class<?> libraryScopeC = cl.loadClass("org.springframework.boot.loader.tools.LibraryScope");
        Class<?> libraryCallbackC = cl.loadClass("org.springframework.boot.loader.tools.LibraryCallback");

        Object repackager = repackagerC.getConstructor(File.class).newInstance(new File(source));
        repackagerC.getMethod("setMainClass", String.class).invoke(repackager, main);
        // We write a distinct dest jar, so leave the source jar untouched (no
        // *.original backup next to it under atomic staging).
        repackagerC.getMethod("setBackupSource", boolean.class).invoke(repackager, false);

        Object compileScope = libraryScopeC.getField("COMPILE").get(null);
        Constructor<?> libCtor = libraryC.getConstructor(File.class, libraryScopeC);
        Method libraryMethod = libraryCallbackC.getMethod("library", libraryC);

        final List<String> libPaths = libs;
        // Libraries is a functional interface loaded by the child loader; a
        // dynamic proxy feeds each dependency jar to loader-tools' callback.
        Object librariesProxy = Proxy.newProxyInstance(
            cl, new Class<?>[] {librariesC},
            (proxy, method, margs) -> {
                if ("doWithLibraries".equals(method.getName())) {
                    Object callback = margs[0];
                    for (String lib : libPaths) {
                        Object library = libCtor.newInstance(new File(lib), compileScope);
                        libraryMethod.invoke(callback, library);
                    }
                    return null;
                }
                // Object methods (toString/hashCode/equals) on the proxy.
                if ("toString".equals(method.getName())) {
                    return "brix-libraries";
                }
                if ("hashCode".equals(method.getName())) {
                    return System.identityHashCode(proxy);
                }
                if ("equals".equals(method.getName())) {
                    return proxy == margs[0];
                }
                return null;
            });

        repackagerC.getMethod("repackage", File.class, librariesC)
            .invoke(repackager, new File(dest), librariesProxy);
        System.out.println("boot jar: " + dest + " (" + libs.size() + " libs)");
    }
}
