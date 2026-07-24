package com.acme;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

/** Exercises Greeter only. Its classpath is greeter.jar and nothing else,
 *  so editing Adder can never make this suite stale. */
class GreeterTest {
    @Test
    void greetsByName() {
        assertEquals("Hello, World!", Greeter.greet("World"));
    }
}
