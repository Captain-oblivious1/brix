// Same as nested.kt but the anonymous OBJECT's body changed — and nothing
// else: this header keeps the file line-for-line aligned with nested.kt, so
// the two synthetic classes differ only in body bytes (same jvm-abi, same
// SMAP lines); only the jar-level enclosure rule can see the change.
@file:JvmName("NestedKt")
package abidemo

inline fun runner(crossinline block: () -> Unit): Runnable {
    return object : Runnable { override fun run() { block(); block() } }
}
