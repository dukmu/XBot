"""Model-facing canonical archive selection, through the ordinary tool registry."""
from XBotv2.core.api.tools import failed_text, succeeded_text


class ArchiveTools:
    def __init__(self, archive, info):
        self.archive = archive
        self.info = info

    async def archive_records(
        self,
        after_seq: int = 0,
        through_seq: int | None = None,
        record_ids: list[str] | None = None,
    ):
        """Attach canonical JSONL records from this conversation as a managed file.

        Sequence selection returns all records after after_seq through a fixed
        confirmed upper bound (inclusive); omit through_seq to capture the
        current durable head. Later writes do not extend the selection.
        Alternatively record_ids selects exactly those IDs in caller order,
        including duplicates. Missing IDs fail; an empty selection is an empty
        file. Use read(path="artifact:<artifact_id>") or the attachment's
        absolute path to inspect it. Raw record payloads retain logical refs.
        """
        try:
            ref = await self.archive.record_selection(
                self.info.session_id, self.info.thread_id,
                after_seq=after_seq, through_seq=through_seq, record_ids=record_ids,
            )
        except ValueError as error:
            return failed_text("archive_selection_invalid", str(error))
        return succeeded_text(
            f"Fixed canonical record selection: {ref.id} ({ref.size} bytes).",
            artifacts=(ref,),
        )
