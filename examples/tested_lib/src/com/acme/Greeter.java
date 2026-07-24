package com.acme;

/** A second unit, deliberately independent of {@link Adder}. */
public class Greeter {
    public static String greet(String who) {
        return "Hello, " + who + "!";
    }
}
