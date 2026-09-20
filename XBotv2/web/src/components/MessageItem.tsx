/* Message chrome adapted from DeepSeek Harness MessageItem/IconActions (MIT). */
import { memo, useCallback, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TimelineEntry } from "../state/runtime";
import { ImageLightbox } from "./ImageLightbox";
import { MessageIconActions } from "./MessageIconActions";
import { ReasoningRow } from "./ReasoningRow";
import styles from "./MessageItem.module.css";

type MessageEntry = Extract<TimelineEntry, { kind: "message" }>;

// Stable plugin array: a fresh array on every render makes react-markdown
// treat the processor as changed and re-parse the message.
const REMARK_PLUGINS = [remarkGfm];

export const MessageItem = memo(function MessageItem({
  entry,
  onRegenerate,
  onBranch,
  branchUnavailable,
}: {
  entry: MessageEntry;
  onRegenerate?: () => Promise<void>;
  onBranch?: () => Promise<void>;
  branchUnavailable?: boolean;
}) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null);
  const closePreview = useCallback(() => setPreview(null), []);
  const user = entry.role === "user";
  // Parsing markdown is the most expensive work in one message; re-parse only
  // when the text actually changes, not on every parent render.
  const markdown = useMemo(
    () => (entry.streaming || !entry.content
      ? null
      : <ReactMarkdown remarkPlugins={REMARK_PLUGINS}>{entry.content}</ReactMarkdown>),
    [entry.content, entry.streaming],
  );
  return (
    <article
      className={`${styles.message} ${user ? styles.user : styles.assistant} message-block ${entry.role}`}
      data-time-hover-root
    >
      {user && <MessageImages entry={entry} onPreview={setPreview} />}
      {entry.reasoning && <ReasoningRow text={entry.reasoning} running={entry.streaming} />}
      {entry.content && (
        entry.streaming
          ? <div className={`${styles.content} ${styles.streaming}`}>{entry.content}</div>
          : <div className={`${styles.content} markdown-body`}>{markdown}</div>
      )}
      {user && entry.deliveryState && (
        <small className={styles.deliveryState} aria-label={`Input ${entry.deliveryState}`}>
          {entry.deliveryState}
        </small>
      )}
      {!entry.streaming && entry.content && (
        <MessageIconActions
          text={entry.content}
          className={user ? styles.actionsEnd : styles.actionsStart}
          onRegenerate={onRegenerate}
          onBranch={onBranch}
          branchUnavailable={branchUnavailable}
        />
      )}
      {!user && <MessageImages entry={entry} onPreview={setPreview} />}
      {preview && <ImageLightbox src={preview.src} alt={preview.alt} onClose={closePreview} />}
      {entry.streaming && !entry.content && !entry.reasoning && (
        <div className="assistant-pending"><i /><i /><i /></div>
      )}
    </article>
  );
}, (previous, next) => (
  // Handler identity is not a render input: the runtime rebuilds these
  // callbacks whenever session state changes (usage, status slots), which used
  // to re-render every visible message — and re-parse its Markdown — on each
  // such event. What matters is whether the action exists, and the callbacks
  // resolve the current session when invoked.
  previous.entry === next.entry
  && Boolean(previous.onRegenerate) === Boolean(next.onRegenerate)
  && Boolean(previous.onBranch) === Boolean(next.onBranch)
  && previous.branchUnavailable === next.branchUnavailable
));

function MessageImages({
  entry,
  onPreview,
}: {
  entry: MessageEntry;
  onPreview: (preview: { src: string; alt: string }) => void;
}) {
  if (!entry.images.length) return null;
  return (
    <div className={styles.images}>
      {entry.images.map((image, index) => image.src ? (
        <button type="button" className={styles.imageButton} key={`${image.label}-${index}`} onClick={() => onPreview({ src: image.src!, alt: image.label })}>
          <img src={image.src} alt={image.label} loading="lazy" />
        </button>
      ) : image.href ? (
        <a className={styles.imageReference} key={`${image.label}-${index}`} href={image.href} target="_blank" rel="noreferrer">{image.label}</a>
      ) : (
        <div className={styles.imageReference} key={`${image.label}-${index}`}>{image.label}</div>
      ))}
    </div>
  );
}
