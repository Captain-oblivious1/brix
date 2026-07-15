import java.io.IOException;
import java.io.Writer;
import java.util.Set;
import javax.annotation.processing.AbstractProcessor;
import javax.annotation.processing.RoundEnvironment;
import javax.annotation.processing.SupportedAnnotationTypes;
import javax.annotation.processing.SupportedSourceVersion;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.Element;
import javax.lang.model.element.TypeElement;

@SupportedAnnotationTypes("Message")
@SupportedSourceVersion(SourceVersion.RELEASE_17)
public final class MessageProcessor extends AbstractProcessor {
    private boolean generated;

    @Override
    public boolean process(
            Set<? extends TypeElement> annotations, RoundEnvironment roundEnvironment) {
        if (generated || roundEnvironment.processingOver()) {
            return true;
        }
        Element annotated = roundEnvironment.getElementsAnnotatedWith(Message.class)
                .stream().findFirst().orElse(null);
        if (annotated == null) {
            return true;
        }
        generated = true;
        String message = annotated.getAnnotation(Message.class).value();
        try (Writer out = processingEnv.getFiler()
                .createSourceFile("GeneratedMessage", annotated).openWriter()) {
            out.write("public final class GeneratedMessage {"
                    + " public static String value() { return \""
                    + message.replace("\\", "\\\\").replace("\"", "\\\"")
                    + "\"; } }\n");
        } catch (IOException error) {
            throw new RuntimeException(error);
        }
        return true;
    }
}
