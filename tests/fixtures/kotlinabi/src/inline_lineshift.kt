// Identical text to inline_base.kt, but the inline fun sits 3 lines lower.
@file:JvmName("InlineKt")
package abidemo
// shift
// shift
// shift
inline fun apply2(a: String, block: (String) -> String): String {
    return block(a + a)
}
