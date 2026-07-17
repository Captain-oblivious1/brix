// A file facade (kind 2) whose top-level function is NOT inline: a body-only
// change (facade_impl.kt) must digest identically. Also covers Kotlin 2.x
// lambdas: they compile via invokedynamic into this facade's body.
@file:JvmName("FacadeKt")
package abidemo

fun combine(a: String, b: String, block: (String) -> String): String {
    return block(a + " & " + b)
}
