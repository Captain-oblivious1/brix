"""Environment sentinels for `ExternalProcessAction(env=...)`.

An ``env`` dict value can be a literal (set the variable + fingerprint it) or a
sentinel that carries a different intent. See the phase-3 spec in
``docs/environment-recording.md`` for the full four-way vocabulary; this module
ships the pieces implemented so far.

``SYSTEM`` — take the variable from the ambient environment and **fingerprint**
it, so a change since the last build re-stales the action, *without* pinning it
to a constant. It desugars to the ``env_used`` tracking mechanism (the JVM
compile still inherits the ambient value; brix just watches it), which is
fingerprint-only, so it is safe on the warm-worker path too (a persistent worker
can be *tracked* but not *set*).

``PASS`` and ``UNSET`` (present, not-fingerprinted; and force-absent) arrive with
the opt-in scrub mode — they only change behaviour once ``env`` is authoritative.
They are not exported yet to avoid shipping a sentinel that is inert.
"""

from __future__ import annotations


class _EnvSentinel:
    """A singleton marker used as an ``env`` dict value."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return f"brix.{self._name}"


# Take the variable from the ambient environment and fingerprint it (track, do
# not pin). Compares by identity (``value is SYSTEM``).
SYSTEM = _EnvSentinel("SYSTEM")
