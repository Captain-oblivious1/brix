// Same API as plain_base.kt, different method bodies.
package abidemo

class Greeter {
    fun greet(name: String): String = "HELLO, " + name.trim() + "!"
    private fun helper(): Int = 2
}
