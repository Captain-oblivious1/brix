package demo.app

import demo.core.Greeter
import demo.core.repeated

// `repeated` is inlined here, so app's bytecode embeds a copy of its body:
// editing that body in `core` changes THIS module's compiled output.
fun main() {
    val greeter = Greeter()
    print(repeated(2) { greeter.greet("brix") })
}
