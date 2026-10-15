#!/usr/bin/env python3
"""Keep console output from dying on a legacy code page.

The Windows console here is GBK (cp936). Python writes Unicode directly to a real
console, so exotic symbols render fine -- right up to the moment stdout is a pipe,
which is what VS Code, a shell pipeline or an agent does. Then stdout is encoded
with the locale code page, and a single ``⇒`` or ``⚠`` raises
``UnicodeEncodeError`` *in the middle of printing something*, which is the worst
possible moment to lose the message.

``install()`` makes the standard streams replace characters the code page cannot
represent instead of raising. It deliberately does **not** change the encoding:
Chinese still renders natively in a GBK console, and UTF-8 streams stay UTF-8.

Tools that print non-ASCII text should call this first. Tools that write *files*
do not need it -- file writes should always be explicit UTF-8.
"""

from __future__ import annotations

import sys


def install() -> None:
    """Make stdout/stderr degrade gracefully instead of raising."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            # Detached or already-consumed stream; nothing to harden.
            pass


__all__ = ["install"]
