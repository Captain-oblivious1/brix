// A Kotlin class with NO inline members: kotlin-abi treats it like jvm-abi,
// so an implementation-only change must digest identically (plain_impl.kt).
// NB: bodies only — adding/renaming even a private member changes @Metadata,
// which both lenses hash (that is Kotlin-conservative, and fine).
package abidemo

class Greeter {
    fun greet(name: String): String = "hello, " + name
    private fun helper(): Int = 1
}
