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
    <div className="catalog-pager" aria-label="Thread pages">
      <span>Thread list snapshot</span>
      <button className="text-button" type="button" disabled={loading || !previousCursor} onClick={() => onPage("previous")}>Previous threads</button>
      <button className="text-button" type="button" disabled={loading || !nextCursor} onClick={() => onPage("next")}>Next threads</button>
      <button className="text-button" type="button" disabled={loading} onClick={() => onPage("refresh")}>Refresh threads</button>
    </div>
  );
}
