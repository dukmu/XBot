import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, ChevronUp, LoaderCircle } from "lucide-react";
import type { TimelineEntry } from "../state/runtime";
import { ConversationNode } from "./ConversationNode";

const TIMELINE_WINDOW = 160;
const TIMELINE_BATCH = 80;
const FOLLOW_THRESHOLD = 32;

interface TimelineProps {
  entries: TimelineEntry[];
  turnRunning: boolean;
  onRetry: () => Promise<void>;
  onBranch: () => Promise<void>;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => Promise<void>;
}

export const Timeline = memo(function Timeline({
  entries,
  turnRunning,
  onRetry,
  onBranch,
  hasOlder,
  loadingOlder,
  onLoadOlder,
}: TimelineProps) {
  const list = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  // Following is explicit user intent, not a pixel measurement: while it
  // holds, new output pins the view to the bottom.  Measuring the distance on
  // every render instead made a single large step (markdown commit, batch
  // flush, image load) look like the user had scrolled away and never
  // resumed following.
  const following = useRef(true);
  const lastScrollTop = useRef(0);
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
  const streaming = entries.some((entry) => entry.kind === "message" && entry.streaming);

  useLayoutEffect(() => {
    const previous = previousLength.current;
    previousLength.current = entries.length;
    setWindowRange((current) => {
      if (current.start < 0) {
        return range;
      }
      if (entries.length > previous && following.current) {
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
    following.current = false;
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
    following.current = true;
    setShowLatest(false);
    setWindowRange({
      start: Math.max(0, entries.length - TIMELINE_WINDOW),
      end: entries.length,
    });
    const element = scrollerOf(list.current);
    element?.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
  };

  useLayoutEffect(() => {
    const element = scrollerOf(list.current);
    if (!element || !following.current) return;
    element.scrollTo({ top: element.scrollHeight });
  }, [entries]);

  useEffect(() => {
    const element = scrollerOf(list.current);
    if (!element) return;
    lastScrollTop.current = element.scrollTop;
    const onScroll = () => {
      const top = element.scrollTop;
      const distance = element.scrollHeight - top - element.clientHeight;
      const movedUp = top < lastScrollTop.current - 1;
      lastScrollTop.current = top;
      if (distance <= FOLLOW_THRESHOLD) {
        following.current = true;
      } else if (movedUp) {
        // Only an upward move means the user left the end.  Growth below the
        // viewport keeps ``scrollTop`` unchanged and must not stop following.
        following.current = false;
      }
      setShowLatest(!following.current || range.end < entries.length);
    };
    const onWheel = (event: WheelEvent) => {
      if (!(event.target instanceof Node) || !list.current?.contains(event.target)) return;
      if (event.deltaY < 0) {
        // Record the user's intent before the browser dispatches the paired
        // scroll event, so a streamed render in between cannot steal focus.
        following.current = false;
        setShowLatest(true);
      }
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    element.addEventListener("wheel", onWheel, { passive: true });
    return () => {
      element.removeEventListener("scroll", onScroll);
      element.removeEventListener("wheel", onWheel);
    };
  }, [entries.length, range.end]);

  useEffect(() => {
    const element = scrollerOf(list.current);
    const content = inner.current;
    if (!element || !content || typeof ResizeObserver === "undefined") return;
    // Height that lands after the render (markdown commit, code highlight,
    // image load) produces no entry change; observe the content box so a
    // following view still reaches the true bottom.
    const observer = new ResizeObserver(() => {
      if (!following.current) return;
      element.scrollTop = element.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      className="timeline"
      ref={list}
    >
      <div className="timeline-inner" ref={inner}>
        {(range.start > 0 || hasOlder) && (
          <button className="timeline-older" type="button" disabled={loadingOlder} onClick={loadEarlier}>
            {loadingOlder ? <LoaderCircle size={14} className="spin" /> : <ChevronUp size={14} />} Older messages
          </button>
        )}
        {visibleEntries.map((entry) => (
          <div className={`timeline-node timeline-node-${entry.kind}`} key={entry.id}>
            <ConversationNode
              entry={entry}
              latestAssistantId={latestAssistant}
              turnRunning={turnRunning}
              onRegenerate={onRetry}
              onBranch={onBranch}
            />
          </div>
        ))}
        {turnRunning && !streaming && (
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
  && previous.turnRunning === next.turnRunning
  && previous.hasOlder === next.hasOlder
  && previous.loadingOlder === next.loadingOlder
  && previous.onRetry === next.onRetry
  && previous.onBranch === next.onBranch
  && previous.onLoadOlder === next.onLoadOlder
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
