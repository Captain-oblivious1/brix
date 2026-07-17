package demo.core

/** A normal function. Callers invoke it through the JVM; they do not embed its
 *  body, so changing this body does not change a caller's bytecode. */
class Greeter {
    fun greet(name: String): String = "Hello, $name!"
}

/** An INLINE function: kotlinc copies its body into every call site at compile
 *  time — line numbers included. So a change to THIS body (or even a comment
 *  added above it) changes the bytecode of every module that calls it, which
 *  is why plain ABI tracking is unsound for Kotlin. The kotlin-abi lens counts
 *  this file's class by bytes, while Greeter above keeps ABI counting. */
inline fun repeated(times: Int, block: () -> String): String {
    val out = StringBuilder()
    repeat(times) { out.append(block()) }
    return out.toString()
}
