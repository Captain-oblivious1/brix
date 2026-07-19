"""Package: backend/services/orders — a Java service (runnable jar).

Independent of ``model`` at the build level, on purpose: in a real Java+TS
monorepo the backend services are separate build units in one repo. What this
package demonstrates alongside the frontend is that lazy loading partitions a
*mixed-language* workspace — a backend edit never loads a frontend package.
"""

from brix import Graph, java


def create_graph() -> Graph:
    g = Graph()
    d = g.data
    g.add(
        java.JarAction(
            source_files=d.glob("backend/services/orders/src/main/java/{name}.java"),
            output_file=d.file("build/backend/orders.jar"),
            classes_dir="build/backend/orders-classes",
            entry_point="com.example.orders.Orders",
        )
    )
    return g
