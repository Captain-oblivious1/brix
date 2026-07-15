import static org.junit.jupiter.api.Assertions.assertEquals;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

class DatabindSmokeTest {
    static final class Value {
        @JsonProperty("renamed")
        public int original = 42;
    }

    @Test
    void bindsThroughCoreAndAnnotations() throws Exception {
        assertEquals("{\"renamed\":42}", new ObjectMapper().writeValueAsString(new Value()));
    }
}
