/* Queue dock adapted from DeepSeek Harness ui-conversation/client/queue/QueueDock.tsx (MIT). */
import { Check, ChevronDown, ChevronUp, Edit3, ListOrdered, Send, Trash2, X } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";
import type { PendingInput } from "../api/types";
import styles from "./QueueDock.module.css";

type QueueAction = { action: "edit"; content: string } | { action: "remove" | "steer" };

// Ported copy: the count header, the row actions, and the two unavailable hints
// are the strings the queue-actions scenario reads.
export const QUEUE_COUNT = (n: number): string => `${n} queued messages`;
export const QUEUE_EDIT = "Edit queued message";
export const QUEUE_EDIT_UNSUPPORTED = "Contains non-text content; editing is not supported yet";
export const QUEUE_SAVE = "Save queued message";
export const QUEUE_CANCEL_EDIT = "Cancel editing";
export const QUEUE_REMOVE = "Remove queued message";
export const QUEUE_STEER = "Steer queued message";
export const QUEUE_STEER_UNAVAILABLE = "Steering is available only while the agent is running";

/**
 * Queue strip above the composer: one queued message renders directly, several
 * default to a collapsible count header, and an empty queue renders nothing.
 *
 * Only `next-turn` items belong here. A message the running turn has taken for
 * its next step leaves the queue and renders as a pending steering row in the
 * transcript instead.
 */
export function QueueDock({
  items,
  running,
  onUpdate,
}: {
  items: PendingInput[];
  running: boolean;
  onUpdate: (messageId: string, action: QueueAction) => Promise<boolean>;
}) {
  const queued = useMemo(() => items.filter((item) => item.target === "next-turn"), [items]);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [collapsed, setCollapsed] = useState(true);
  const listId = useId();

  useEffect(() => {
    if (queued.length === 0) setCollapsed(true);
    if (editing && !queued.some((item) => item.message_id === editing.id)) setEditing(null);
  }, [editing, queued]);

  if (queued.length === 0) return null;
  const interactionActive = Boolean(editing || busy);
  const expanded = !collapsed || interactionActive;
  const listVisible = queued.length === 1 || expanded;

  const apply = async (messageId: string, action: QueueAction) => {
    setBusy(messageId);
    try {
      return await onUpdate(messageId, action);
    } finally {
      setBusy("");
    }
  };

  const save = async () => {
    if (!editing || !editing.text.trim()) return;
    if (await apply(editing.id, { action: "edit", content: editing.text.trim() })) {
      setEditing(null);
    }
  };

  return (
    <section className={styles.dock} aria-label="Queued messages" data-queue-dock>
      <div className={styles.panel}>
        {queued.length > 1 && (
          <button
            type="button"
            className={styles.header}
            aria-controls={listId}
            aria-expanded={expanded}
            disabled={interactionActive}
            onClick={() => setCollapsed((value) => !value)}
          >
            <ListOrdered size={14} />
            <span>{QUEUE_COUNT(queued.length)}</span>
            {expanded ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
          </button>
        )}
        <ul id={listId} className={styles.list} hidden={!listVisible}>
          {listVisible && queued.map((item) => (
            <li key={item.message_id} className={styles.row}>
              {queued.length === 1 && <ListOrdered size={14} className={styles.lead} />}
              {editing?.id === item.message_id ? (
                <input
                  autoFocus
                  className={styles.editor}
                  aria-label={QUEUE_EDIT}
                  value={editing.text}
                  onChange={(event) => setEditing({ id: item.message_id, text: event.currentTarget.value })}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setEditing(null);
                    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      void save();
                    }
                  }}
                />
              ) : (
                <span className={styles.preview}>{preview(item)}</span>
              )}
              <div className={styles.actions}>
                {editing?.id === item.message_id ? (
                  <>
                    <Action label={QUEUE_SAVE} disabled={Boolean(busy) || !editing.text.trim()} onClick={() => void save()}><Check size={14} /></Action>
                    <Action label={QUEUE_CANCEL_EDIT} disabled={Boolean(busy)} onClick={() => setEditing(null)}><X size={14} /></Action>
                  </>
                ) : (
                  <>
                    <Action
                      label={QUEUE_EDIT}
                      // A row without text has nothing to edit. The hint stays a
                      // native title because a disabled button fires no hover.
                      title={editable(item) ? undefined : QUEUE_EDIT_UNSUPPORTED}
                      disabled={Boolean(busy) || !editable(item)}
                      onClick={() => setEditing({ id: item.message_id, text: item.content })}
                    >
                      <Edit3 size={14} />
                    </Action>
                    <Action label={QUEUE_REMOVE} disabled={Boolean(busy)} onClick={() => void apply(item.message_id, { action: "remove" })}><Trash2 size={14} /></Action>
                    <Action
                      label={QUEUE_STEER}
                      title={running ? undefined : QUEUE_STEER_UNAVAILABLE}
                      disabled={Boolean(busy) || !running}
                      onClick={() => void apply(item.message_id, { action: "steer" })}
                    >
                      <Send size={14} />
                    </Action>
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Action({ label, title, disabled, onClick, children }: {
  label: string;
  title?: string;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return <button type="button" className={styles.action} aria-label={label} title={title ?? label} disabled={disabled} onClick={onClick}>{children}</button>;
}

/** A queued row is editable only while it still carries text. */
function editable(item: PendingInput): boolean {
  return item.content.trim() !== "";
}

function preview(item: PendingInput): string {
  if (item.content) return item.content;
  const count = item.image_count + item.artifact_count;
  return count === 1 ? "1 attachment" : `${count} attachments`;
}
