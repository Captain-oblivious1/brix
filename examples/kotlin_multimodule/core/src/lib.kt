package demo.core

/** A normal function. Callers invoke it through the JVM; they do not embed its
 *  body, so changing this body does not change a caller's bytecode. */
class Greeter {
    fun greet(name: String): String = "Hello, $name!"
}

/** An INLINE function: kotlinc copies its body into every call site at compile
 *  time. So a change to THIS body changes the bytecode of every module that
 *  calls it — which is exactly why a Kotlin dependency cannot be tracked by its
 *  ABI (method signatures) the way a Java one can. `brix.kotlin` byte-tracks
 *  Kotlin classpath entries for this reason. */
inline fun repeated(times: Int, block: () -> String): String {
    val out = StringBuilder()
    repeat(times) { out.append(block()) }
    return out.toString()
}
