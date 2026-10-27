"""Explicit canonical rollout exchange owned by the conversation domain."""

from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4
from XBotv2.agentloop.api.contracts import InboxItem
from XBotv2.agentloop.impl.storage import input_message
from XBotv2.agentloop.impl.storage_projection import MESSAGE
from XBotv2.session.api.storage import ThreadFacts
from XBotv2.session.api.contracts import new_session_id
from XBotv2.core.api.artifacts import ArtifactRef, ArtifactKind
from XBotv2.core.api.messages import (
    HumanInputMessage,
    RuntimeNoticeMessage,
    ToolMessage,
)
from XBotv2.core.api.parts import ImagePart
from XBotv2.core.api.tools import ToolSucceeded, ToolFailed
from XBotv2.persistence.api.files import run_file_io as io


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as output:
        output.write(encoded(value))
        output.flush()
        os.fsync(output.fileno())


def _publish(source, target):
    if target.exists():
        raise FileExistsError(target)
    os.rename(source, target)
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def _record_refs(store, record):
    if record["kind"] == "artifact_referenced":
        return (ArtifactRef.model_validate(record["data"]["artifact"]),)
    return tuple(ref for message in await _messages(store, record) for ref in _refs(message))


async def _messages(store, record):
    kind, data = record["kind"], record["data"]
    if kind == "input_accepted":
        return (input_message(InboxItem.model_validate(data["item"]), {}),)
    if kind == "message":
        return (MESSAGE.validate_python(data["message"]),)
    if kind == "surface_replaced":
        return tuple(MESSAGE.validate_python(value) for value in data["messages"])
    if kind == "input_edited":
        return (input_message(await store.input(record["id"]), {}),)
    if kind == "input_committed":
        return (await store.message(data["input_id"]),)
    return ()


def _refs(message):
    result = []
    if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
        result.extend(message.artifacts)
        result.extend(
            ArtifactRef(
                id=p.image.artifact_id,
                kind=ArtifactKind.MEDIA,
                media_type=p.image.media_type,
                size=p.image.size,
            )
            for p in message.parts
            if isinstance(p, ImagePart)
        )
    if isinstance(message, ToolMessage) and isinstance(
        message.outcome, (ToolSucceeded, ToolFailed)
    ):
        result.extend(message.outcome.output.artifacts)
    return result


async def _export_artifact(archive, session_id, thread_id, ref, objects):
    from XBotv2.persistence.api.files import write_text_atomic
    target = objects / ref.id
    if target.exists():
        return
    artifacts = archive.backend.artifacts(session_id, thread_id)
    await artifacts.export_reference(ref, target, supplied_streams=(thread_id,))
    source = await artifacts.selection_stream(ref)
    if source is None:
        return
    dependencies = target / "agentloop-refs.jsonl"
    await io(write_text_atomic, dependencies, "")
    owner = archive._store(session_id, source)
    if await owner.thread_repository.load() is None:
        return
    async for record in artifacts.read_records(ref):
        for nested in await _record_refs(owner, record):
            await io(_write, dependencies, nested.model_dump(mode="json"))
            await _export_artifact(archive, session_id, thread_id, nested, objects)


def _dependency(path, offset, budget):
    with path.open("rb") as reader:
        reader.seek(offset)
        return _next(reader, budget), reader.tell()


async def _import_artifact(archive, session_id, thread_id, ref, objects):
    artifacts = archive.backend.artifacts(session_id, thread_id)
    await artifacts.import_reference(objects / ref.id, ref)
    if await artifacts.selection_stream(ref) is None:
        return
    offset = 0
    while True:
        value, offset = await io(
            _dependency, objects / ref.id / "agentloop-refs.jsonl", offset,
            archive.backend.record_bytes,
        )
        if value is None:
            break
        nested = ArtifactRef.model_validate(value)
        await _import_artifact(archive, session_id, thread_id, nested, objects)
        owner = archive._store(session_id, thread_id)
        await owner.sql(lambda sql: sql.execute(
            "INSERT OR REPLACE INTO conversation_artifacts VALUES(?,?,?)",
            (thread_id, nested.id, nested.model_dump_json()),
        ), write=True, size=len(nested.model_dump_json().encode("utf-8")))


async def export_rollout(archive, session_id, thread_id, destination):
    destination = Path(destination).expanduser().resolve()
    objects = destination.with_name(destination.name + ".objects")
    if destination.exists() or objects.exists():
        raise FileExistsError(destination)
    store = archive._store(session_id, thread_id)
    facts = await store.thread_repository.load()
    if facts is None:
        raise ValueError("Cannot export an unknown thread")
    temporary = destination.with_name("." + destination.name + "." + uuid4().hex)
    temp_objects = temporary.with_name(temporary.name + ".objects")
    copied = False
    try:
        cut = (await archive.flush(session_id, thread_id))[0]
        await io(
            _write,
            temporary,
            {
                "kind": "thread_facts",
                "facts": facts.model_dump(mode="json"),
                "source_session": session_id,
                "source_stream": thread_id,
                "cut_seq": cut,
                "objects": objects.name,
            },
        )
        offset = 0
        while offset < cut:
            async with store.operation():
                page, last, _ = await store.stream.read_range(
                    after_seq=offset,
                    through_seq=cut,
                    limit=16,
                    max_bytes=archive.backend.record_bytes,
                )
                if not page:
                    raise ValueError("Export prefix has a missing record")
                for record in page:
                    for ref in await _record_refs(store, record):
                        await _export_artifact(
                            archive, session_id, thread_id, ref, temp_objects,
                        )
                    await io(
                        _write, temporary, {"kind": "rollout_record", "record": record}
                    )
                offset = last
        if temp_objects.exists():
            await io(_publish, temp_objects, objects)
            copied = True
        await io(_publish, temporary, destination)
        return destination
    except BaseException:
        if copied:
            await io(shutil.rmtree, objects, True)
        raise
    finally:
        await io(temporary.unlink, True)
        await io(shutil.rmtree, temp_objects, True)


def _next(reader, limit):
    data = reader.readline(limit + 1)
    if not data:
        return None
    if len(data) > limit or not data.endswith(b"\n"):
        raise ValueError("Incomplete or oversized canonical JSONL entry")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Invalid canonical JSONL entry") from error
    if not isinstance(value, dict):
        raise ValueError("Canonical JSONL entry must be an object")
    return value


async def import_rollout(archive, source, *, workspace_root=None, artifact_source=None):
    source = Path(source).expanduser().resolve()
    target_id = new_session_id()
    async with archive.backend.prepare_session(target_id) as prepared:
        reader = await io(source.open, "rb")
        try:
            header = await io(_next, reader, archive.backend.record_bytes)
            if (
                header is None
                or set(header)
                != {
                    "kind",
                    "facts",
                    "source_session",
                    "source_stream",
                    "cut_seq",
                    "objects",
                }
                or header["kind"] != "thread_facts"
            ):
                raise ValueError("Expected canonical thread_facts header")
            facts = ThreadFacts.model_validate(header["facts"])
            if facts.parent_thread_id:
                raise ValueError(
                    "A single-thread import cannot reference an absent parent"
                )
            if workspace_root is not None:
                facts = facts.model_copy(
                    update={
                        "workspace_root": str(
                            Path(workspace_root).expanduser().resolve()
                        )
                    }
                )
            if not Path(facts.workspace_root).is_dir():
                raise ValueError("Import requires an existing workspace")
            name = header["objects"]
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("Artifact directory must be a basename")
            objects = (
                Path(artifact_source).resolve()
                if artifact_source is not None
                else source.parent / name
            )
            thread_id = header["source_stream"]
            repository = archive.sessions.thread_repository(target_id, thread_id)
            await repository.remember(facts)
            await repository.ensure()
            store = archive._store(target_id, thread_id)
            seq = 0
            while (
                value := await io(_next, reader, archive.backend.record_bytes)
            ) is not None:
                if (
                    set(value) != {"kind", "record"}
                    or value["kind"] != "rollout_record"
                ):
                    raise ValueError("Expected canonical rollout record")
                record = value["record"]
                seq += 1
                if (
                    set(record) != {"seq", "id", "kind", "data"}
                    or type(record["seq"]) is not int
                    or record["seq"] != seq
                ):
                    raise ValueError("Non-contiguous canonical record sequence")
                async with store.operation(write=True):
                    await store.stream.append(
                        record["kind"], record["data"], identity=record["id"]
                    )
                    for ref in await _record_refs(store, record):
                        await _import_artifact(
                            archive, target_id, thread_id, ref, objects,
                        )
            if seq != header["cut_seq"]:
                raise ValueError("Export prefix is incomplete")
            async with store.operation(write=True):
                # Dependency metadata was staged in this unpublished target.
                # Back it with owner facts after the original prefix so every
                # source sequence/offset selection keeps its original meaning.
                after_artifact = ""
                while True:
                    references = await store.sql(lambda sql: sql.execute(
                        "SELECT artifact_id,metadata FROM conversation_artifacts WHERE thread_id=? AND artifact_id>? ORDER BY artifact_id LIMIT 128",
                        (thread_id, after_artifact),
                    ))
                    if not references:
                        break
                    for _, metadata in references:
                        await store.stream.append("artifact_referenced", {"artifact": json.loads(metadata)})
                    after_artifact = references[-1][0]
                pending = await store.pending_ids()
                await store.stream.append(
                    "import_boundary",
                    {
                        "source_session": header["source_session"],
                        "source_stream": header["source_stream"],
                        "source_seq": seq,
                        "input_ids": pending,
                    },
                )
        finally:
            await io(reader.close)
        await prepared.publish()
        return target_id
