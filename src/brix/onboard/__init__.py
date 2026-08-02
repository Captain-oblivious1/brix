"""brix onboarder — bring an existing build under brix.

An onboarder has two halves joined by a tool-neutral JSON **IR**:

* a *dumper* (frontend) that introspects an incumbent build's already-resolved
  model and emits the IR — one per source tool (Gradle today; Maven/CMake/Make
  are meant to emit the same schema);
* an *emitter* (backend, not yet built) that reads the IR and writes
  ``brix_build.py``, mapping recognized nodes to native brix atoms and bridging
  the rest back to the source tool.

The IR is deliberately shaped by brix's needs (modules + nodes), not by any one
build system, and carries a per-node ``fidelity`` flag and a tool-neutral
``bridge`` descriptor so the emitter and its verify pass work the same across
frontends. See ``brix.onboard.gradle`` for the first dumper.
"""
