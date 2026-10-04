"""Internal metadata attached to durable conversation messages."""

from __future__ import annotations

from time import time as wall_time
from typing import Any, List, Mapping, MutableMapping, Optional, TypeVar


# These fields describe Mercury' durable record, not provider-visible message
# content. They must not influence context-pressure decisions.
PERSISTENCE_ONLY_MESSAGE_FIELDS = frozenset({"timestamp"})

_Message = TypeVar("_Message", bound=MutableMapping[str, Any])


def stamp_message_timestamp(
    message: _Message,
    *,
    timestamp: Optional[float] = None,
) -> _Message:
    """Attach a creation timestamp without replacing source-provided time.

    Gateway adapters can supply the platform event time. All other callers use
    the local wall clock at the point the message enters the live transcript.
    Returning the same mapping keeps the helper convenient at append sites.
    """
    if message.get("timestamp") is None:
        message["timestamp"] = wall_time() if timestamp is None else timestamp
    return message


def append_message(
    messages: list[Any],
    message: _Message,
    *,
    timestamp: Optional[float] = None,
) -> _Message:
    """Stamp and append one live transcript message."""
    stamp_message_timestamp(message, timestamp=timestamp)
    messages.append(message)
    return message


# The durable per-message id (``messages.message_uid``): minted once at the row's first insert and kept by
# every host copy of that logical message (in-place compaction generation, rotation child, concurrent-tail
# clone, replace re-issue, rewrite in place). Unlike ``_row_id`` (a physical id re-issued per copy, opt-in on
# restore) it is restored unconditionally, so context engines can key on it across restarts and boundaries.
MESSAGE_UID = "message_uid"
# The merge witness on a consecutive-user merge survivor: the ``message_uid`` of each absorbed row, in
# absorption order (the uid sibling of ``_absorbed_row_ids``; persisted as ``messages.absorbed_message_uids``).
# The survivor keeps the FIRST constituent's uid.
ABSORBED_MESSAGE_UIDS = "_absorbed_message_uids"


def message_uid_or_none(msg: Mapping[str, Any]) -> Optional[str]:
    """The dict's ``message_uid`` when it is a non-empty string, else ``None`` (never coerced: an int or a
    blank would be a bug upstream of the write, not an identity)."""
    uid = msg.get(MESSAGE_UID)
    return uid if isinstance(uid, str) and uid else None


def uid_list(value: Any) -> List[str]:
    """The unique non-empty string uids of a live list, in order; anything else is ``[]``."""
    return list(dict.fromkeys(u for u in (value if isinstance(value, list) else ()) if isinstance(u, str) and u))


def record_absorbed_message(
    survivor: MutableMapping[str, Any], dropped: Mapping[str, Any], *, dropped_leads: bool = False,
) -> None:
    """Merge-witness bookkeeping for every host fold of *dropped* into *survivor*.

    The composite keeps the uid of the constituent whose text comes first and records every other
    constituent's uid in ``_absorbed_message_uids`` (text order, no repeats). By default the survivor's
    text leads; with *dropped_leads* the dropped dict's text was put first (the real user anchor folded
    into a scaffolding turn), so its uid becomes the survivor's and the survivor's former uid is recorded.
    A dict without a uid (unflushed, scaffolding, engine-authored) contributes nothing; an empty result
    leaves the survivor untouched.
    """
    survivor_uid = message_uid_or_none(survivor)
    dropped_uid = message_uid_or_none(dropped)
    own = uid_list(survivor.get(ABSORBED_MESSAGE_UIDS))
    theirs = uid_list(dropped.get(ABSORBED_MESSAGE_UIDS))
    if dropped_leads and dropped_uid:
        survivor[MESSAGE_UID] = dropped_uid
        ordered = theirs + ([survivor_uid] if survivor_uid else []) + own
    else:
        ordered = own + ([dropped_uid] if dropped_uid else []) + theirs
    if absorbed := [uid for uid in dict.fromkeys(ordered) if uid != survivor.get(MESSAGE_UID)]:
        survivor[ABSORBED_MESSAGE_UIDS] = absorbed
