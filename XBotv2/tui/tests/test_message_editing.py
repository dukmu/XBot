from XBotv2.agentloop.api.models import PendingInputData
from XBotv2.core.api.domain import InboxTarget
from XBotv2.session.api.records import DisplayBodyPage
from XBotv2.tui.impl.view.composer import Composer
from XBotv2.tui.impl.view.selection import SelectionPanel
from XBotv2.tui.tests.factories import ScriptedBackend, human_record, snapshot, tui_app


async def pause(pilot):
    await pilot.pause()
    await pilot.app.controller.flush()
    await pilot.pause()


async def test_queue_edit_saves_and_cancel_restores_draft():
    item = PendingInputData(message_id="q", content="original", target=InboxTarget.NEXT_TURN)
    backend = ScriptedBackend(session=snapshot(pending_inputs=(item,)))
    calls = []
    async def update(*args, **kwargs):
        calls.append((args, kwargs))
    backend.update_pending_input = update
    app = tui_app(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await pause(pilot)
        composer = app.query_one(Composer)
        composer.load_text("draft")
        await pilot.press("ctrl+q", "enter")
        await pause(pilot)
        await pilot.press("enter")
        await pause(pilot)
        assert composer.text == "original"
        composer.load_text("changed")
        await pilot.press("escape")
        assert composer.text == "draft"
        assert not calls
        await pilot.press("ctrl+q", "enter")
        await pause(pilot)
        await pilot.press("enter")
        await pause(pilot)
        composer.load_text("updated")
        await pilot.press("enter")
        await pause(pilot)
        assert calls[0][1] == {"action": "edit", "content": "updated"}
        assert composer.text == "draft"


async def test_recall_reads_complete_message_body():
    content = "abc" * 500
    backend = ScriptedBackend(session=snapshot(history=[human_record("u1", content)]))
    async def body(session, thread, message, *, field, offset=0, limit=4096):
        return DisplayBodyPage(content=content[offset:], offset=offset, total=len(content), next_offset=None)
    backend.read_display_body = body
    app = tui_app(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await pause(pilot)
        await pilot.press("ctrl+r")
        await pause(pilot)
        assert app.query_one(SelectionPanel)
        await pilot.press("enter")
        await pause(pilot)
        assert app.query_one(Composer).text == content
        assert not backend.sent


async def test_failed_queue_edit_preserves_text_and_original_draft():
    item = PendingInputData(message_id="q", content="original", target=InboxTarget.NEXT_TURN)
    backend = ScriptedBackend(session=snapshot(pending_inputs=(item,)))
    async def update(*args, **kwargs):
        raise RuntimeError("already claimed")
    backend.update_pending_input = update
    app = tui_app(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await pause(pilot)
        composer = app.query_one(Composer)
        composer.load_text("draft")
        await pilot.press("ctrl+q", "enter")
        await pause(pilot)
        await pilot.press("enter")
        await pause(pilot)
        composer.load_text("my edit")
        await pilot.press("enter")
        await pause(pilot)
        assert composer.text == "my edit"
        await pilot.press("escape")
        assert composer.text == "draft"


async def test_queue_with_unidentified_artifacts_cannot_be_edited():
    item = PendingInputData(message_id="q", content="preview", target=InboxTarget.NEXT_TURN, artifact_count=1)
    backend = ScriptedBackend(session=snapshot(pending_inputs=(item,)))
    app = tui_app(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await pause(pilot)
        composer = app.query_one(Composer)
        composer.load_text("draft")
        await pilot.press("ctrl+q", "enter")
        await pause(pilot)
        await pilot.press("enter")
        await pause(pilot)
        assert composer.text == "draft"
        assert app._queue_edit is None


async def test_recall_reads_externalized_original():
    from XBotv2.core.api.artifacts import ArtifactKind, ArtifactRef

    ref = ArtifactRef(id="context/original.txt", name="original-user-input.txt", kind=ArtifactKind.CONTEXT)
    message = human_record("u1", "externalized preview").model_copy(update={"artifacts": (ref,)})
    backend = ScriptedBackend(session=snapshot(history=[message]))
    reads = []
    async def read_artifact(session, thread, artifact_id):
        reads.append(artifact_id)
        return "complete 原文".encode("utf-8")
    backend.read_artifact = read_artifact
    app = tui_app(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await pause(pilot)
        await pilot.press("ctrl+r", "enter")
        await pause(pilot)
        assert app.query_one(Composer).text == "complete 原文"
        assert reads == [ref.id]


async def test_editing_hint_is_persistent_and_preserves_attached_draft_image():
    from XBotv2.agentloop.api.models import MediaInput

    item = PendingInputData(message_id="q", content="original", target=InboxTarget.NEXT_TURN)
    backend = ScriptedBackend(session=snapshot(pending_inputs=(item,)))
    calls = []
    async def update(*args, **kwargs):
        calls.append(kwargs)
    backend.update_pending_input = update
    app = tui_app(backend)
    async with app.run_test(size=(80, 24)) as pilot:
        await pause(pilot)
        composer = app.query_one(Composer)
        image = MediaInput(data="abc", media_type="image/png", name="x.png")
        app.controller.attach(image)
        composer.load_text("draft")
        await pilot.press("ctrl+q", "enter")
        await pause(pilot)
        await pilot.press("enter")
        await pause(pilot)
        assert "Editing queued input" in composer.hint_text
        assert not composer.input.may_submit_empty
        app.controller._dirty = True
        await pause(pilot)
        assert "Editing queued input" in composer.hint_text
        composer.load_text("edit")
        await pilot.press("enter")
        await pause(pilot)
        assert calls == [{"action": "edit", "content": "edit"}]
        assert composer.text == "draft"
        assert app.controller.attachments == (image,)
        assert "Editing queued input" not in composer.hint_text


