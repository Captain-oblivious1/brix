package abidemo;
public class Lib {
    public static final int LIMIT = 10;
    private int state = 0;
    public int compute(String s) { return s.length() + state; }
    public String name() { return "lib"; }
    private void tick() { state++; }
}
