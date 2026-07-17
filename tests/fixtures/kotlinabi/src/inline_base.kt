// A file facade with an INLINE top-level function: kotlin-abi must count this
// class by bytes, so inline_bodychange.kt AND inline_lineshift.kt (which only
// shifts line numbers — inlined code carries them into consumers via SMAP)
// must both digest differently.
@file:JvmName("InlineKt")
package abidemo

inline fun apply2(a: String, block: (String) -> String): String {
    return block(a + a)
}
