// An inline property ACCESSOR (IS_INLINE_ACCESSOR in getter_flags): the
// class must count by bytes.
package abidemo

class Holder(val raw: String) {
    val shouted: String inline get() = raw.uppercase()
}
