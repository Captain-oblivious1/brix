#!/usr/bin/env bash
# Resolve spring-petclinic's dependency classpaths with Maven and write them
# where brix.py reads them. brix consumes this resolution rather than
# reimplementing Maven; run once (first run needs network to populate ~/.m2).
#
# Runs against the checkout (default ~/Projects/brix_test/spring-petclinic,
# override with BRIX_PETCLINIC_ROOT) and writes the classpath files there,
# where brix.py reads them.
#
# Outputs (machine-specific ~/.m2 paths — not committed):
#   brix-cp-compile.txt        compile-scope jars (javac)
#   brix-cp-runtime.txt        runtime-scope jars (launch / BOOT-INF/lib)
#   brix-cp-loader-tools.txt   spring-boot-loader-tools + deps (repackager)
set -euo pipefail
cd "${BRIX_PETCLINIC_ROOT:-$HOME/Projects/brix_test/spring-petclinic}"
./mvnw -q dependency:build-classpath -DincludeScope=compile -Dmdep.outputFile=brix-cp-compile.txt
./mvnw -q dependency:build-classpath -DincludeScope=runtime -Dmdep.outputFile=brix-cp-runtime.txt

# spring-boot-loader-tools drives the executable-jar repackaging (see
# brix.springboot). Resolve it and its transitive jars into a classpath the
# BootRepackager helper loads at run time. Version-pinned to the Spring Boot
# parent (read from pom.xml) so the loader matches the app's runtime.
BOOT_VERSION=$(sed -n '/<parent>/,/<\/parent>/{s/.*<version>\(.*\)<\/version>.*/\1/p}' pom.xml | head -1)
cat > /tmp/brix-loader-tools-pom.xml <<XML
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>brix</groupId><artifactId>lt-cp</artifactId><version>1</version>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-loader-tools</artifactId>
      <version>${BOOT_VERSION}</version>
    </dependency>
  </dependencies>
</project>
XML
./mvnw -q -f /tmp/brix-loader-tools-pom.xml dependency:build-classpath \
  -Dmdep.outputFile="$(pwd)/brix-cp-loader-tools.txt"

echo "compile jars:      $(tr ':' '\n' < brix-cp-compile.txt | grep -c '\.jar$')"
echo "runtime jars:      $(tr ':' '\n' < brix-cp-runtime.txt | grep -c '\.jar$')"
echo "loader-tools jars: $(tr ':' '\n' < brix-cp-loader-tools.txt | grep -c '\.jar$')"
