// Same API as facade_base.kt, different body (including the lambda-ish part).
@file:JvmName("FacadeKt")
package abidemo

fun combine(a: String, b: String, block: (String) -> String): String {
    return block(b + " | " + a)
}
