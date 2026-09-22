/* Layout and copy ported from DeepSeek Harness (MIT): the details column is
 * empty until a tool row in the message flow is picked, and says so. */
import { X } from "lucide-react";
import type { ToolEntry } from "../state/runtime";
import { ToolArtifacts } from "./ToolArtifacts";
import { CHAT_SEARCH_MAX_LINES, SearchCard, searchCard } from "./SearchCard";
import { ToolOutput } from "./ToolOutput";

export const DETAILS_EMPTY_HINT = "Click a tool row in the message flow to view its details";

export function DetailsPanel({
  tool,
  onClose,
}: {
  tool: ToolEntry | null;
  onClose: () => void;
}) {
  // A search call keeps its grouped card here too: the ported model derives it
  // once and both render sites use it. This column is the single-call reading
  // surface, so it keeps the fuller cap.
  const card = tool === null ? null : searchCard(tool);
  return (
    <aside className="details-panel" aria-label="Details" data-testid="details-panel">
      <header className="details-panel-head">
        <span>Details</span>
        <button
          type="button"
          className="icon-button"
          aria-label="Close details"
          title="Close details"
          onClick={onClose}
        >
          <X size={14} />
        </button>
      </header>
      {tool === null ? (
        <p className="details-panel-empty">{DETAILS_EMPTY_HINT}</p>
      ) : (
        <div className="details-panel-body">
          <div className="details-panel-title">
            <span className={`tool-status-icon status-${tool.status}`} aria-hidden />
            <strong>{tool.name}</strong>
            <span className="details-panel-status">{tool.status}</span>
          </div>
          {tool.error ? <ToolOutput value={tool.error} label="Error" /> : null}
          <ToolOutput value={tool.args} label="Arguments" />
          {card !== null ? (
            <SearchCard {...card} maxLines={CHAT_SEARCH_MAX_LINES * 2} />
          ) : tool.result !== null && tool.result !== "" ? (
            <ToolOutput value={tool.result} label="Result" />
          ) : null}
          <ToolArtifacts artifacts={tool.artifacts} />
        </div>
      )}
    </aside>
  );
}
