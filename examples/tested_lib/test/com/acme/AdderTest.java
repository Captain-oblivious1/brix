package com.acme;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

/** Exercises Adder only. Its classpath is adder.jar and nothing else. */
class AdderTest {
    @Test
    void addsTwoPositives() {
        assertEquals(5, Adder.add(2, 3));
    }

    @Test
    void addsANegative() {
        assertEquals(-1, Adder.add(2, -3));
    }
}
