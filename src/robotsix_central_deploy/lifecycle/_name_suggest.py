"""Fuzzy service-name suggestions, shared by the operator and chat planes.

Callers routinely address the deploy plane by a component's *repository*
name (``robotsix-invest``) when it is registered under a short name
(``invest``).  A bare "not found" reads like a permissions problem: it cost
most of a session before the mismatch was spotted, twice — once on the
operator plane (fixed by the 404 suggestion in ``deps/dependencies.py``) and
once on the chat plane, which returned the same bare message.

The matcher lives here, source-agnostic, so both planes phrase the answer the
same way: the operator plane feeds it ``ServiceRecord`` names, the chat plane
feeds it ``ComponentConfig`` ids.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable

#: Never list more than this many candidates — a long list is noise, and the
#: message is read by an agent that has to act on it.
MAX_SUGGESTIONS = 3

#: ``difflib`` similarity floor.  0.6 is its own default; lower admits pairs
#: that share little more than a letter.
_CUTOFF = 0.6


def suggest_names(name: str, known: Iterable[str]) -> list[str]:
    """Return up to :data:`MAX_SUGGESTIONS` names close to *name*, best first.

    Substring matches are collected *before* the ratio-based matches and are
    what make the reported case work at all: ``robotsix-invest`` and
    ``invest`` share no prefix, so ``difflib`` alone scores the pair below
    any usable cutoff.
    """
    candidates = [k for k in known if k]
    if not candidates:
        return []
    lowered = name.lower()
    substring = [k for k in candidates if lowered in k.lower() or k.lower() in lowered]
    close = difflib.get_close_matches(
        name, candidates, n=MAX_SUGGESTIONS, cutoff=_CUTOFF
    )
    # dict.fromkeys keeps substring hits first while dropping duplicates.
    return list(dict.fromkeys(substring + close))[:MAX_SUGGESTIONS]


def with_suggestions(detail: str, name: str, known: Iterable[str]) -> str:
    """Append ``; did you mean 'a', 'b'?`` to *detail* when there is a match.

    Returns *detail* unchanged when nothing is close enough, so the caller
    can build the message unconditionally.
    """
    suggestions = suggest_names(name, known)
    if not suggestions:
        return detail
    quoted = ", ".join(f"'{s}'" for s in suggestions)
    return f"{detail}; did you mean {quoted}?"
