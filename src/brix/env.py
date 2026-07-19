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

``UNSET`` forces a variable **absent** from the process environment (even when
the ambient environment would supply it), so a build can declare "I must not be
affected by this variable". It is part of the action's identity, so flipping a
variable between `UNSET` and any other intent re-stales the action.

``PASS`` lets a variable **through** the environment *without* fingerprinting it —
for plumbing a tool needs to run (``PATH``, ``HOME``) whose value is noisy and
machine-specific, so tracking it would break cross-machine caching. Under **scrub**
(the opt-in authoritative-env mode) it is what keeps such a variable present; with
scrub off it is a no-op (the variable is inherited anyway, untracked).

The build-wide **base map** (`brix.env.BASE_ENV`) supplies the universal plumbing
as ``PASS`` so a scrubbed build still runs; each language module adds its
behaviour vars as ``SYSTEM`` on top (docs/environment-recording.md).
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

# Force the variable absent from the process environment. Compares by identity
# (``value is UNSET``).
UNSET = _EnvSentinel("UNSET")

# Let the variable through (present, from the ambient value) but do NOT
# fingerprint it — for plumbing whose value must not enter the cache key.
PASS = _EnvSentinel("PASS")

# Universal plumbing every action needs to run; supplied as PASS so a scrubbed
# build still finds its tools/caches without those noisy values busting the
# fingerprint. Applied under scrub; a no-op with scrub off (already inherited).
BASE_ENV = {
    "PATH": PASS,
    "HOME": PASS,
    "TMPDIR": PASS,
}
