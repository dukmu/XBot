import { Check, ChevronDown, ChevronUp, Edit3, ListOrdered, Send, Trash2, X } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";
import type { PendingInput } from "../api/types";
import styles from "./QueueDock.module.css";

type QueueAction = { action: "edit"; content: string } | { action: "remove" | "steer" };

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
  const steering = useMemo(() => items.filter((item) => item.target === "next-step"), [items]);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [collapsed, setCollapsed] = useState(true);
  const listId = useId();

  useEffect(() => {
    if (queued.length === 0) setCollapsed(true);
    if (editing && !queued.some((item) => item.message_id === editing.id)) setEditing(null);
  }, [editing, queued]);

  if (items.length === 0) return null;
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
            <span>{queued.length} queued</span>
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
                  aria-label="Edit queued message"
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
                    <Action label="Save queued message" disabled={Boolean(busy) || !editing.text.trim()} onClick={() => void save()}><Check size={14} /></Action>
                    <Action label="Cancel editing" disabled={Boolean(busy)} onClick={() => setEditing(null)}><X size={14} /></Action>
                  </>
                ) : (
                  <>
                    <Action label="Edit queued message" disabled={Boolean(busy)} onClick={() => setEditing({ id: item.message_id, text: item.content })}><Edit3 size={14} /></Action>
                    <Action label="Remove queued message" disabled={Boolean(busy)} onClick={() => void apply(item.message_id, { action: "remove" })}><Trash2 size={14} /></Action>
                    <Action label="Steer queued message" disabled={Boolean(busy) || !running} onClick={() => void apply(item.message_id, { action: "steer" })}><Send size={14} /></Action>
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
        {steering.map((item) => (
          <div className={styles.steering} key={item.message_id}>
            <Send size={13} /><span>{preview(item)}</span><small>Steering</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function Action({ label, disabled, onClick, children }: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return <button type="button" className={styles.action} aria-label={label} title={label} disabled={disabled} onClick={onClick}>{children}</button>;
}

function preview(item: PendingInput): string {
  if (item.content) return item.content;
  const count = item.image_count + item.artifact_count;
  return count === 1 ? "1 attachment" : `${count} attachments`;
}
