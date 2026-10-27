"""Agentloop owns typed rollout facts and all conversation projections."""

from pydantic import TypeAdapter
from XBotv2.core.api.messages import (
    ConversationMessage,
    HumanInputMessage,
    RuntimeNoticeMessage,
)
from XBotv2.agentloop.api.contracts import InboxItem
from XBotv2.persistence.api.models import StreamCorrupt

MESSAGE = TypeAdapter(ConversationMessage)


def _one(sql, query, parameters=()):
    rows = sql.execute(query, parameters)
    return rows[0] if rows else None


def _all(sql, query, parameters=()):
    return sql.execute(query, parameters)


def _many(sql, query, parameters):
    for row in parameters:
        sql.execute(query, row)


def _changed(sql, query, parameters=()):
    sql.execute(query, parameters)
    return sql.changed


def _order(connection, table, thread):
    return _one(
        connection,
        f"SELECT COALESCE(MAX(order_key),0)+1 FROM {table} WHERE thread_id=?",
        (thread,),
    )[0]


def _add_message(connection, thread, message_id, record_id, ordinal=0):
    connection.execute(
        "INSERT INTO message_refs VALUES(?,?,?,?)",
        (thread, message_id, record_id, ordinal),
    )
    connection.execute(
        "UPDATE history_state SET surface_version=surface_version+1 WHERE thread_id=?",
        (thread,),
    )
    for table in ("surface", "transcript"):
        connection.execute(
            f"INSERT INTO {table} VALUES(?,?,?)",
            (thread, _order(connection, table, thread), message_id),
        )


def _replace(connection, table, thread, sources, replacements):
    if not sources:
        raise StreamCorrupt("Replacement has no sources")
    # References only, never archived bodies. Most operations touch W; the
    # transcript range is located by its first identity and checked in SQL.
    first = _one(
        connection,
        f"SELECT order_key FROM {table} WHERE thread_id=? AND record_id=?",
        (thread, sources[0]),
    )
    if first is None:
        raise StreamCorrupt(f"{table} source is not current")
    start = first[0]
    checked = 0
    after = start - 1
    end = start - 1
    while checked < len(sources):
        current = _all(
            connection,
            f"SELECT order_key,record_id FROM {table} WHERE thread_id=? AND order_key>? ORDER BY order_key LIMIT ?",
            (thread, after, min(256, len(sources) - checked)),
        )
        if (
            not current
            or [row[1] for row in current] != sources[checked : checked + len(current)]
        ):
            raise StreamCorrupt(f"{table} replacement sources are not contiguous")
        checked += len(current)
        after = current[-1][0]
        end = after
    connection.execute(
        f"DELETE FROM {table} WHERE thread_id=? AND order_key BETWEEN ? AND ?",
        (thread, start, end),
    )
    # Offset then renumber only following references, without fetching H.
    shift = len(replacements) - (end - start + 1)
    connection.execute(
        f"UPDATE {table} SET order_key=-order_key WHERE thread_id=? AND order_key>?",
        (thread, end),
    )
    connection.execute(
        f"UPDATE {table} SET order_key=-order_key+? WHERE thread_id=? AND order_key<0",
        (shift, thread),
    )
    _many(
        connection,
        f"INSERT INTO {table} VALUES(?,?,?)",
        ((thread, start + i, value) for i, value in enumerate(replacements)),
    )


def _replace_transcript(connection, thread, sources, replacements):
    marks = ",".join("(?)" for _ in sources)
    cte = f"""WITH RECURSIVE initial(id) AS (VALUES {marks}),
    ancestry(id) AS (SELECT id FROM initial UNION ALL SELECT l.source_id FROM lineage l JOIN ancestry a ON l.summary_id=a.id WHERE l.thread_id=?),
    leaves(id) AS (SELECT id FROM ancestry WHERE NOT EXISTS(SELECT 1 FROM lineage WHERE thread_id=? AND summary_id=ancestry.id)) """
    parameters = (*sources, thread, thread)
    count = _one(connection, cte + "SELECT COUNT(*) FROM leaves", parameters)[0]
    row = _one(
        connection,
        cte
        + "SELECT MIN(t.order_key),MAX(t.order_key),COUNT(*) FROM transcript t JOIN leaves l ON t.record_id=l.id WHERE t.thread_id=?",
        (*parameters, thread),
    )
    start, end, found = row
    if not count or found != count or end - start + 1 != count:
        raise StreamCorrupt("Transcript ancestry is missing or not contiguous")
    connection.execute(
        "DELETE FROM transcript WHERE thread_id=? AND order_key BETWEEN ? AND ?",
        (thread, start, end),
    )
    shift = len(replacements) - count
    connection.execute(
        "UPDATE transcript SET order_key=-order_key WHERE thread_id=? AND order_key>?",
        (thread, end),
    )
    connection.execute(
        "UPDATE transcript SET order_key=-order_key+? WHERE thread_id=? AND order_key<0",
        (shift, thread),
    )
    _many(
        connection,
        "INSERT INTO transcript VALUES(?,?,?)",
        ((thread, start + i, value) for i, value in enumerate(replacements)),
    )


def project(connection, thread, records):
    for record, offset, length in records:
        seq, identity, kind, data = (
            record["seq"],
            record["id"],
            record["kind"],
            record["data"],
        )
        if kind == "input_accepted":
            item = InboxItem.model_validate(data["item"])
            connection.execute(
                "INSERT INTO input_ids VALUES(?,?,?,?,0)",
                (thread, item.id, identity, identity),
            )
            connection.execute(
                "INSERT INTO inbox VALUES(?,?,?,?,?,?,?)",
                (
                    item.id,
                    thread,
                    item.target.value,
                    item.input.kind,
                    getattr(item.input, "source", ""),
                    identity,
                    seq,
                ),
            )
        elif kind == "input_edited":
            row = _one(
                connection,
                "SELECT current_record,retired FROM input_ids WHERE thread_id=? AND input_id=?",
                (thread, data["input_id"]),
            )
            if row is None or row[1] or row[0] != data["previous"]:
                raise StreamCorrupt("Edit does not refer to the current pending input")
            if not data["content"].strip():
                raise StreamCorrupt("Empty input edit")
            connection.execute(
                "UPDATE input_ids SET current_record=? WHERE thread_id=? AND input_id=?",
                (identity, thread, data["input_id"]),
            )
            connection.execute(
                "UPDATE inbox SET body_ref=? WHERE thread_id=? AND input_id=?",
                (identity, thread, data["input_id"]),
            )
        elif kind == "input_retargeted":
            if (
                _changed(
                    connection,
                    "UPDATE inbox SET delivery=?,order_key=? WHERE thread_id=? AND input_id=?",
                    (data["target"], seq, thread, data["input_id"]),
                )
                != 1
            ):
                raise StreamCorrupt("Retargeted input is not pending")
        elif kind in (
            "input_removed",
            "inputs_discarded",
            "inputs_consumed",
            "fork_boundary",
            "import_boundary",
        ):
            for input_id in data["input_ids"]:
                if (
                    _one(
                        connection,
                        "SELECT 1 FROM input_ids WHERE thread_id=? AND input_id=?",
                        (thread, input_id),
                    )
                    is None
                ):
                    raise StreamCorrupt("Retired input does not exist")
                connection.execute(
                    "DELETE FROM inbox WHERE thread_id=? AND input_id=?",
                    (thread, input_id),
                )
                connection.execute(
                    "UPDATE input_ids SET retired=1 WHERE thread_id=? AND input_id=?",
                    (thread, input_id),
                )
        elif kind == "input_committed":
            current = _one(
                connection,
                "SELECT current_record,retired FROM input_ids WHERE thread_id=? AND input_id=?",
                (thread, data["input_id"]),
            )
            if current is None or current[1] or current[0] != data["body_ref"]:
                raise StreamCorrupt("Committed input is not pending/current")
            _add_message(connection, thread, data["input_id"], identity)
            connection.execute(
                "DELETE FROM inbox WHERE thread_id=? AND input_id=?",
                (thread, data["input_id"]),
            )
            connection.execute(
                "UPDATE input_ids SET retired=1 WHERE thread_id=? AND input_id=?",
                (thread, data["input_id"]),
            )
        elif kind == "message":
            message = MESSAGE.validate_python(data["message"])
            _add_message(connection, thread, message.id, identity)
            if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
                connection.execute(
                    "INSERT INTO input_ids VALUES(?,?,?,?,1)",
                    (thread, message.id, identity, identity),
                )
        elif kind == "surface_replaced":
            replacements = [
                MESSAGE.validate_python(value) for value in data["messages"]
            ]
            for ordinal, message in enumerate(replacements):
                connection.execute(
                    "INSERT INTO message_refs VALUES(?,?,?,?)",
                    (thread, message.id, identity, ordinal),
                )
            ids = [message.id for message in replacements]
            _replace(connection, "surface", thread, data["source_ids"], ids)
            if data["preserve_transcript"]:
                if len(ids) != 1:
                    raise StreamCorrupt("Preserving replacement requires one summary")
                _many(
                    connection,
                    "INSERT INTO lineage VALUES(?,?,?,?)",
                    (
                        (thread, ids[0], i, source)
                        for i, source in enumerate(data["source_ids"])
                    ),
                )
            else:
                _replace_transcript(connection, thread, data["source_ids"], ids)
                connection.execute(
                    "UPDATE history_state SET transcript_revision=transcript_revision+1 WHERE thread_id=?",
                    (thread,),
                )
            connection.execute(
                "UPDATE history_state SET surface_revision=surface_revision+1,surface_version=surface_version+1 WHERE thread_id=?",
                (thread,),
            )
        elif kind == "turn_started":
            if (
                _one(
                    connection,
                    "SELECT 1 FROM message_refs WHERE stream_id=? AND message_id=?",
                    (thread, data["input_id"]),
                )
                is None
            ):
                raise StreamCorrupt("Turn starts before its input is committed")
            connection.execute(
                "INSERT INTO turn_starts VALUES(?,?)", (thread, data["input_id"])
            )
            connection.execute(
                "UPDATE history_state SET turn_count=turn_count+1 WHERE thread_id=?",
                (thread,),
            )
        elif kind == "event":
            event = data["event"]
            transaction = event["transaction"]
            if event["kind"] == "transaction_started":
                connection.execute(
                    "INSERT INTO open_transactions VALUES(?,?,?)",
                    (thread, transaction["kind"], transaction["id"]),
                )
            elif event["kind"] == "transaction_ended":
                connection.execute(
                    "DELETE FROM open_transactions WHERE thread_id=? AND kind=? AND transaction_id=?",
                    (thread, transaction["kind"], transaction["id"]),
                )
            else:
                raise StreamCorrupt("Invalid durable event")
        else:
            raise StreamCorrupt(f"Unknown rollout kind: {kind}")
