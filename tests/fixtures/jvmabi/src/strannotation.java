package abidemo;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
// A String-valued annotation element (like apiguardian's @API(since="...")).
// CLASS retention so it lands in RuntimeInvisibleAnnotations, which jvm-abi
// includes. This is the case that crashed the parser on real JUnit classes:
// the 's' element value indexes a Utf8 constant directly.
public class Lib {
    public static final int LIMIT = 10;
    private int state = 0;
    @Since("1.13") public int compute(String s) { return s.length() + state; }
    public String name() { return "lib"; }
    private void tick() { state++; }
}
@Retention(RetentionPolicy.CLASS)
@interface Since { String value(); }
