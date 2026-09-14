import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.PosixFileAttributeView;
import java.nio.file.attribute.PosixFileAttributes;
import java.util.List;
import java.util.stream.Stream;
import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.transform.OutputKeys;
import javax.xml.transform.Transformer;
import javax.xml.transform.TransformerFactory;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import org.w3c.dom.Document;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;
import org.xml.sax.SAXException;
import org.xml.sax.SAXParseException;
import org.xml.sax.helpers.DefaultHandler;

final class RemoveArchivedApiTokens {
    private static final String TOKEN_PROPERTY = "jenkins.security.ApiTokenProperty";

    private RemoveArchivedApiTokens() {}

    public static void main(String[] arguments) {
        if (arguments.length != 1) {
            System.exit(2);
        }
        try {
            removeArchivedApiTokens(Path.of(arguments[0]));
        } catch (Exception exception) {
            System.err.println("archived API token removal failed");
            System.exit(1);
        }
    }

    private static void removeArchivedApiTokens(Path users) throws Exception {
        BasicFileAttributes usersAttributes;
        try {
            usersAttributes = Files.readAttributes(
                users, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS
            );
        } catch (NoSuchFileException exception) {
            return;
        }
        if (!usersAttributes.isDirectory()) {
            throw new IOException("users root is not a directory");
        }

        List<Path> archivedPaths;
        try (Stream<Path> paths = Files.walk(users)) {
            archivedPaths = paths.sorted().toList();
        }
        if (archivedPaths.stream().anyMatch(Files::isSymbolicLink)) {
            throw new IOException("users tree contains a symbolic link");
        }
        List<Path> configs = archivedPaths.stream()
            .filter(path -> path.getFileName().toString().equals("config.xml"))
            .toList();
        for (Path config : configs) {
            if (!Files.isRegularFile(config, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("user configuration is not a regular file");
            }
            removeTokenProperty(config);
        }
        for (Path stats : archivedPaths.stream()
                .filter(path -> path.getFileName().toString().equals("apiTokenStats.xml"))
                .toList()) {
            if (!Files.isRegularFile(stats, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("API token statistics are not a regular file");
            }
            Files.delete(stats);
        }
        for (Path config : configs) {
            if (parse(config).getElementsByTagName(TOKEN_PROPERTY).getLength() != 0) {
                throw new IOException("archived API token property remains");
            }
        }
    }

    private static Document parse(Path config) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
        factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
        factory.setXIncludeAware(false);
        factory.setExpandEntityReferences(false);
        DocumentBuilder builder = factory.newDocumentBuilder();
        builder.setErrorHandler(new DefaultHandler() {
            @Override
            public void warning(SAXParseException exception) throws SAXException {
                throw exception;
            }

            @Override
            public void error(SAXParseException exception) throws SAXException {
                throw exception;
            }

            @Override
            public void fatalError(SAXParseException exception) throws SAXException {
                throw exception;
            }
        });
        return builder.parse(config.toFile());
    }

    private static void removeTokenProperty(Path config) throws Exception {
        Document document = parse(config);
        NodeList properties = document.getElementsByTagName(TOKEN_PROPERTY);
        if (properties.getLength() == 0) {
            return;
        }
        while (properties.getLength() != 0) {
            Node property = properties.item(0);
            property.getParentNode().removeChild(property);
        }

        PosixFileAttributes attributes = Files.readAttributes(
            config, PosixFileAttributes.class, LinkOption.NOFOLLOW_LINKS
        );
        Path temporary = Files.createTempFile(
            config.getParent(), "." + config.getFileName() + ".", ".tmp"
        );
        try {
            TransformerFactory factory = TransformerFactory.newInstance();
            factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_STYLESHEET, "");
            Transformer transformer = factory.newTransformer();
            transformer.setOutputProperty(OutputKeys.ENCODING, "UTF-8");
            transformer.setOutputProperty(OutputKeys.VERSION, document.getXmlVersion());
            try (OutputStream output = Files.newOutputStream(temporary)) {
                transformer.transform(new DOMSource(document), new StreamResult(output));
            }
            Files.setPosixFilePermissions(temporary, attributes.permissions());
            Files.setOwner(temporary, attributes.owner());
            Files.getFileAttributeView(temporary, PosixFileAttributeView.class)
                .setGroup(attributes.group());
            try {
                Files.move(
                    temporary,
                    config,
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING
                );
            } catch (AtomicMoveNotSupportedException exception) {
                throw new IOException(
                    "atomic user configuration replacement is unavailable", exception
                );
            }
        } finally {
            Files.deleteIfExists(temporary);
        }
    }
}
