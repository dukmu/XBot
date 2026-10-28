import { CircleAlert, Info } from "lucide-react";
import type { TimelineEntry } from "../state/runtime";
import { ContextInjectionRow } from "./ContextInjectionRow";
import { MessageItem } from "./MessageItem";
import { ToolCall } from "./ToolCall";

export function ConversationNode({
  entry,
  latestAssistantId,
  turnRunning,
  onRegenerate,
  onBranch,
  selectedToolId,
  onSelectTool,
  skillTools,
}: {
  entry: TimelineEntry;
  latestAssistantId: string;
  turnRunning: boolean;
  onRegenerate: () => Promise<void>;
  onBranch: () => Promise<void>;
  selectedToolId?: string;
  onSelectTool?: (tool: TimelineEntry) => void;
  /** Skill tool names from the catalog: those rows use the ported skill view. */
  skillTools?: readonly string[];
}) {
  if (entry.kind === "message") {
    const assistant = entry.role === "assistant";
    return (
      <MessageItem
        entry={entry}
        onRegenerate={assistant && !turnRunning && entry.id === latestAssistantId
          ? onRegenerate
          : undefined}
        onBranch={assistant ? onBranch : undefined}
        branchUnavailable={turnRunning || entry.id !== latestAssistantId}
      />
    );
  }
  if (entry.kind === "tool") {
    return (
      <ToolCall
        tool={entry}
        selected={selectedToolId === entry.toolCallId}
        onSelect={(tool) => onSelectTool?.(tool)}
        skill={skillTools?.includes(entry.name) ?? false}
      />
    );
  }
  if (entry.kind === "runtime") return <ContextInjectionRow entry={entry} />;
  return (
    <div className={`notice-row ${entry.level}`} role="status">
      {entry.level === "error" ? <CircleAlert size={14} /> : <Info size={14} />}
      <span>{entry.content}</span>
    </div>
  );
}
