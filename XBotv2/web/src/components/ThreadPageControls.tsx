export function ThreadPageControls({
  previousCursor,
  nextCursor,
  loading,
  onPage,
}: {
  previousCursor: string | null;
  nextCursor: string | null;
  loading: boolean;
  onPage: (direction: "previous" | "next" | "refresh") => void;
}) {
  return (
    <div className="workspace-inline-action" aria-label="Thread pages">
      <span>Thread list snapshot</span>
      <button type="button" disabled={loading || !previousCursor} onClick={() => onPage("previous")}>Previous threads</button>
      <button type="button" disabled={loading || !nextCursor} onClick={() => onPage("next")}>Next threads</button>
      <button type="button" disabled={loading} onClick={() => onPage("refresh")}>Refresh threads</button>
    </div>
  );
}
