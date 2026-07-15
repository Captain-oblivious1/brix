import static org.junit.jupiter.api.Assertions.assertEquals;

import com.fasterxml.jackson.annotation.JsonProperty;
import org.junit.jupiter.api.Test;

class AnnotationsSmokeTest {
    static final class Value {
        @JsonProperty("renamed")
        public String original;
    }

    @Test
    void exposesAnnotationValues() throws Exception {
        JsonProperty property = Value.class.getField("original").getAnnotation(JsonProperty.class);
        assertEquals("renamed", property.value());
    }
}
