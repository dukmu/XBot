import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, CircleAlert, ChevronUp, LoaderCircle, Terminal } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { TimelineEntry } from "../state/runtime";
import { MessageItem } from "./MessageItem";
import { ToolCall } from "./ToolCall";

const TIMELINE_WINDOW = 160;
const TIMELINE_BATCH = 80;

interface TimelineProps {
  entries: TimelineEntry[];
  assistantDraft: Extract<TimelineEntry, { kind: "message" }> | null;
  turnRunning: boolean;
  onRetry: () => Promise<void>;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => Promise<void>;
}

export const Timeline = memo(function Timeline({
  entries,
  assistantDraft,
  turnRunning,
  onRetry,
  hasOlder,
  loadingOlder,
  onLoadOlder,
}: TimelineProps) {
  const list = useRef<HTMLDivElement>(null);
  const shouldFollow = useRef(true);
  const previousLength = useRef(0);
  const pendingPrependHeight = useRef<number | null>(null);
  const [windowRange, setWindowRange] = useState({ start: -1, end: -1 });
  const [showLatest, setShowLatest] = useState(false);
  const latestAssistant = latestAssistantId(entries);
  const range = windowRange.start < 0
    ? { start: Math.max(0, entries.length - TIMELINE_WINDOW), end: entries.length }
    : {
      start: Math.min(windowRange.start, entries.length),
      end: Math.min(windowRange.end, entries.length),
    };
  const visibleEntries = useMemo(
    () => entries.slice(range.start, range.end),
    [entries, range.start, range.end],
  );

  useLayoutEffect(() => {
    const previous = previousLength.current;
    previousLength.current = entries.length;
    setWindowRange((current) => {
      if (current.start < 0) {
        return range;
      }
      if (entries.length > previous && shouldFollow.current) {
        return {
          start: Math.max(0, entries.length - TIMELINE_WINDOW),
          end: entries.length,
        };
      }
      if (entries.length < current.end) {
        return {
          start: Math.min(current.start, entries.length),
          end: entries.length,
        };
      }
      return current;
    });
  }, [entries.length, range.start, range.end]);

  useLayoutEffect(() => {
    const previousHeight = pendingPrependHeight.current;
    const element = scrollerOf(list.current);
    if (previousHeight === null || !element) return;
    pendingPrependHeight.current = null;
    element.scrollTop += element.scrollHeight - previousHeight;
  }, [entries.length, range.start, range.end]);

  const loadEarlier = () => {
    const element = scrollerOf(list.current);
    if (!element || (range.start <= 0 && !hasOlder)) return;
    shouldFollow.current = false;
    setShowLatest(true);
    pendingPrependHeight.current = element.scrollHeight;
    if (range.start <= 0) {
      void onLoadOlder();
      return;
    }
    setWindowRange((current) => {
      const start = Math.max(0, current.start - TIMELINE_BATCH);
      return { start, end: Math.min(entries.length, start + TIMELINE_WINDOW) };
    });
  };

  const scrollToLatest = () => {
    shouldFollow.current = true;
    setShowLatest(false);
    setWindowRange({
      start: Math.max(0, entries.length - TIMELINE_WINDOW),
      end: entries.length,
    });
    const element = scrollerOf(list.current);
    element?.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
  };

  useEffect(() => {
    const element = scrollerOf(list.current);
    if (shouldFollow.current && element) element.scrollTo({ top: element.scrollHeight });
  }, [entries, assistantDraft?.content, assistantDraft?.reasoning]);

  useEffect(() => {
    const element = scrollerOf(list.current);
    if (!element) return;
    const onScroll = () => {
      const following = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
      shouldFollow.current = following;
      setShowLatest(!following || range.end < entries.length);
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, [entries.length, range.end]);

  return (
    <div
      className="timeline"
      ref={list}
    >
      <div className="timeline-inner">
        {(range.start > 0 || hasOlder) && (
          <button className="timeline-older" type="button" disabled={loadingOlder} onClick={loadEarlier}>
            {loadingOlder ? <LoaderCircle size={14} className="spin" /> : <ChevronUp size={14} />} Older messages
          </button>
        )}
        {visibleEntries.map((entry) => {
          if (entry.kind === "message") {
            return <MessageItem key={entry.id} entry={entry} canRetry={!turnRunning && entry.role === "assistant" && entry.id === latestAssistant} onRetry={onRetry} />;
          }
          if (entry.kind === "tool") return <ToolCall key={entry.id} tool={entry} />;
          if (entry.kind === "runtime") {
            return (
              <details key={entry.id} className="runtime-context">
                <summary>
                  <Terminal size={14} />
                  <span>Injected context</span>
                  <code>{entry.source} · {entry.event}</code>
                  <ChevronRight size={13} className="summary-chevron" />
                </summary>
                <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.content}</ReactMarkdown></div>
              </details>
            );
          }
          return (
            <div key={entry.id} className={`notice-row ${entry.level}`}>
              {entry.level === "error" ? <CircleAlert size={14} /> : <Terminal size={14} />}
              <span>{entry.content}</span>
            </div>
          );
        })}
        {assistantDraft && (
          <MessageItem entry={assistantDraft} canRetry={false} onRetry={onRetry} />
        )}
        {turnRunning && !assistantDraft && (
          <div className="turn-pending"><LoaderCircle size={15} className="spin" /> Working</div>
        )}
      </div>
      {showLatest && (
        <button className="timeline-latest" type="button" aria-label="Jump to latest activity" onClick={scrollToLatest}>
          <ChevronRight size={14} className="timeline-latest-icon" /> Latest activity
        </button>
      )}
    </div>
  );
}, (previous, next) => (
  previous.entries === next.entries
  && previous.assistantDraft === next.assistantDraft
  && previous.turnRunning === next.turnRunning
  && previous.hasOlder === next.hasOlder
  && previous.loadingOlder === next.loadingOlder
));

function scrollerOf(list: HTMLDivElement | null): HTMLElement | null {
  return list?.closest<HTMLElement>("[data-conversation-scroll]") ?? list;
}

function latestAssistantId(entries: TimelineEntry[]): string {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (entry.kind === "message" && entry.role === "assistant" && !entry.streaming) return entry.id;
  }
  return "";
}
