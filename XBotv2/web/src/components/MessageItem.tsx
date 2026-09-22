/* Message chrome adapted from DeepSeek Harness MessageItem/IconActions (MIT). */
import { memo, useCallback, useMemo, useState } from "react";
import { TriangleAlert } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { remarkDshGrammar } from "../markdown/remarkDshGrammar";
import { MarkdownImage } from "../markdown/MarkdownImage";
// KaTeX's own stylesheet, exactly as the ported renderer loads it.
import "katex/dist/katex.min.css";
import type { TimelineEntry } from "../state/runtime";
import { ImageLightbox } from "./ImageLightbox";
import { MessageIconActions } from "./MessageIconActions";
import { ReasoningRow } from "./ReasoningRow";
import styles from "./MessageItem.module.css";

type MessageEntry = Extract<TimelineEntry, { kind: "message" }>;

// Stable plugin arrays: a fresh array on every render makes react-markdown
// treat the processor as changed and re-parse the message. The ported grammar
// extensions (CJK-friendly strong emphasis, backslash math delimiters) load
// before `remarkMath` registers dollar math, as in the ported parser.
const REMARK_PLUGINS = [remarkGfm, remarkDshGrammar, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];
// Markdown images follow the ported media policy instead of the stock element.
const MARKDOWN_COMPONENTS = { img: MarkdownImage };

/** Ported turn footer: the timing a settled reply ran with. */
export const TURN_FOOTER_RAN = "Ran for";
export const TURN_FOOTER_TTFT = "TTFT";

/** `850 ms` / `1.5 s`, the ported footer's duration shape. */
export function formatTurnDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "";
  return ms >= 1_000 ? `${(ms / 1_000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/** The ported notice: title and hint for a reply the output limit cut off. */
export const OUTPUT_LIMIT_TITLE = "Output token limit reached";
export const OUTPUT_LIMIT_HINT =
  'The reply was cut off; earlier output is preserved in the conversation. Send "continue" to let the model resume.';

const TRUNCATING_STOP_REASONS = new Set(["length", "max_tokens", "max_output_tokens"]);

export function truncated(stopReason: string | undefined): boolean {
  return stopReason !== undefined && TRUNCATING_STOP_REASONS.has(stopReason);
}

export const MessageItem = memo(function MessageItem({
  entry,
  onRegenerate,
  onBranch,
  branchUnavailable,
  pendingSteering = false,
}: {
  entry: MessageEntry;
  onRegenerate?: () => Promise<void>;
  onBranch?: () => Promise<void>;
  branchUnavailable?: boolean;
  /** Host-authoritative pre-admission steering projection, not a durable row. */
  pendingSteering?: boolean;
}) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null);
  const closePreview = useCallback(() => setPreview(null), []);
  const user = entry.role === "user";
  const footer = turnFooter(entry);
  // Parsing markdown is the most expensive work in one message; re-parse only
  // when the text actually changes, not on every parent render.
  const markdown = useMemo(
    () => (entry.streaming || !entry.content
      ? null
      : (
        <ReactMarkdown
          remarkPlugins={REMARK_PLUGINS}
          rehypePlugins={REHYPE_PLUGINS}
          components={MARKDOWN_COMPONENTS}
        >
          {entry.content}
        </ReactMarkdown>
      )),
    [entry.content, entry.streaming],
  );
  return (
    <article
      className={`${styles.message} ${user ? styles.user : styles.assistant} message-block ${entry.role}`}
      data-time-hover-root
      data-pending-steering={pendingSteering || undefined}
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
      {!user && truncated(entry.stopReason) && (
        <div className="message-truncation" role="status" data-testid="output-limit-notice">
          <TriangleAlert size={13} />
          <div>
            <strong>{OUTPUT_LIMIT_TITLE}</strong>
            <small>{OUTPUT_LIMIT_HINT}</small>
          </div>
        </div>
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
      {!user && !entry.streaming && footer !== "" && (
        // The ported footer: what the reply ran for.  Its clock and throughput
        // have no XBot source (messages carry no timestamp, usage is per
        // session), so only the durations the record holds are shown.
        <div className="message-turn-footer" data-testid="turn-footer">{footer}</div>
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
  && previous.pendingSteering === next.pendingSteering
));

/** `Ran for 1.5 s · TTFT 368 ms`, or nothing when the record carries no timing. */
function turnFooter(entry: MessageEntry): string {
  const timing = entry.timing;
  if (!timing || entry.role !== "assistant") return "";
  const parts: string[] = [];
  const total = formatTurnDuration(timing.llm_ms);
  if (total) parts.push(`${TURN_FOOTER_RAN} ${total}`);
  const ttft = formatTurnDuration(timing.ttft_ms ?? Number.NaN);
  if (ttft) parts.push(`${TURN_FOOTER_TTFT} ${ttft}`);
  return parts.join(" · ");
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
