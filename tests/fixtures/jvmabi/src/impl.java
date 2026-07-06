package abidemo;
public class Lib {
    public static final int LIMIT = 10;
    private int state = 5;
    public int compute(String s) { tick(); return s.length() * 3 + state; }
    public String name() { return "lib"; }
    private void tick() { state += 2; }
}
