/* Message chrome adapted from DeepSeek Harness MessageItem/IconActions (MIT). */
import { Clipboard, ClipboardCheck, RotateCcw } from "lucide-react";
import { memo, useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TimelineEntry } from "../state/runtime";
import { ImageLightbox } from "./ImageLightbox";
import { ReasoningRow } from "./ReasoningRow";
import styles from "./MessageItem.module.css";

type MessageEntry = Extract<TimelineEntry, { kind: "message" }>;

export const MessageItem = memo(function MessageItem({
  entry,
  canRetry,
  onRetry,
}: {
  entry: MessageEntry;
  canRetry: boolean;
  onRetry: () => Promise<void>;
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
        <MessageActions
          text={entry.content}
          align={user ? "end" : "start"}
          onRetry={entry.role === "assistant" && canRetry ? onRetry : undefined}
        />
      )}
      {!user && <MessageImages entry={entry} onPreview={setPreview} />}
      {preview && <ImageLightbox src={preview.src} alt={preview.alt} onClose={closePreview} />}
      {entry.streaming && !entry.content && !entry.reasoning && (
        <div className="assistant-pending"><i /><i /><i /></div>
      )}
    </article>
  );
}, (previous, next) => previous.entry === next.entry && previous.canRetry === next.canRetry);

function MessageActions({
  text,
  align,
  onRetry,
}: {
  text: string;
  align: "start" | "end";
  onRetry?: () => Promise<void>;
}) {
  const [copied, setCopied] = useState(false);
  const pending = useRef(false);
  const timer = useRef<number | null>(null);
  const epoch = useRef(0);

  useEffect(() => () => {
    epoch.current += 1;
    pending.current = false;
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  const copy = useCallback(async () => {
    if (copied || pending.current || !navigator.clipboard) return;
    const currentEpoch = epoch.current;
    pending.current = true;
    try {
      await navigator.clipboard.writeText(text);
      if (currentEpoch !== epoch.current) return;
      setCopied(true);
      timer.current = window.setTimeout(() => {
        timer.current = null;
        setCopied(false);
      }, 1000);
    } catch {
      // Clipboard access can be denied by the browser; leave the action retryable.
    } finally {
      if (currentEpoch === epoch.current) pending.current = false;
    }
  }, [copied, text]);

  return (
    <div className={styles.actions} data-align={align}>
      <button type="button" aria-label={copied ? "Copied" : "Copy message"} title={copied ? "Copied" : "Copy message"} onClick={() => void copy()}>
        {copied ? <ClipboardCheck size={16} /> : <Clipboard size={16} />}
      </button>
      {onRetry && (
        <button type="button" aria-label="Regenerate response" title="Regenerate response" onClick={() => void onRetry()}>
          <RotateCcw size={16} />
        </button>
      )}
    </div>
  );
}

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
