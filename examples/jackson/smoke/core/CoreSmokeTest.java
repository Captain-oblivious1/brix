import static org.junit.jupiter.api.Assertions.assertEquals;

import com.fasterxml.jackson.core.JsonFactory;
import org.junit.jupiter.api.Test;

class CoreSmokeTest {
    @Test
    void parsesJsonTokens() throws Exception {
        try (var parser = new JsonFactory().createParser("{\"answer\":42}")) {
            assertEquals("{", parser.nextToken().asString());
            assertEquals("answer", parser.nextFieldName());
            assertEquals(42, parser.nextIntValue(-1));
        }
    }
}
