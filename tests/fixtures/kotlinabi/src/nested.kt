// An inline fun containing an anonymous object: kotlinc emits a synthetic
// nested class (NestedKt$runner$1) that consumers COPY when inlining. Its own
// metadata (kind 3) says nothing about inline — the jar-level enclosure rule
// (nested under an inline-flagged name) must byte-count it.
@file:JvmName("NestedKt")
package abidemo

inline fun runner(crossinline block: () -> Unit): Runnable {
    return object : Runnable { override fun run() = block() }
}
