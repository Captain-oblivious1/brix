// Same signature as inline_base.kt, different inline body.
@file:JvmName("InlineKt")
package abidemo

inline fun apply2(a: String, block: (String) -> String): String {
    return block(a + a + a)
}
