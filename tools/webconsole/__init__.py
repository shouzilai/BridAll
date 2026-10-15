"""Local web console for the Workbench tokenized logging pipeline.

Layout:

- ``devices``     read-only COM port registry and non-intrusive probes
- ``runstore``    append-only run directories holding every piece of evidence
- ``assertions``  deterministic smoke assertions over captured evidence
- ``pipeline``    the state machine that owns PlatformIO and the COM port
- ``server``      local HTTP + SSE service consumed by ``static/``
"""
