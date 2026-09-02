/* Message chrome adapted from DeepSeek Harness MessageItem/IconActions (MIT). */
import { memo, useCallback, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TimelineEntry } from "../state/runtime";
import { ImageLightbox } from "./ImageLightbox";
import { MessageIconActions } from "./MessageIconActions";
import { ReasoningRow } from "./ReasoningRow";
import styles from "./MessageItem.module.css";

type MessageEntry = Extract<TimelineEntry, { kind: "message" }>;

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
          : <div className={`${styles.content} markdown-body`}><ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown></div>
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
  previous.entry === next.entry
  && previous.onRegenerate === next.onRegenerate
  && previous.onBranch === next.onBranch
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
