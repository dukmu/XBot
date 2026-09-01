/* Adapted from DeepSeek Harness ContextMeter.tsx (MIT). */
import { useEffect, useRef, useState } from "react";
import type { UsageData } from "../api/types";
import css from "./ContextMeter.module.css";

const RADIUS = 5.5;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

export function ContextMeter({ usage, contextWindow }: { usage: UsageData; contextWindow: number }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  const available = contextWindow > 0;
  const percent = available
    ? Math.max(0, Math.min(100, Math.round(usage.context_tokens / contextWindow * 100)))
    : 0;
  const cache = usage.cache_read_input_tokens + usage.cache_creation_input_tokens + usage.prompt_cache_write_tokens;

  useEffect(() => {
    if (!open || !available) return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && root.current?.contains(event.target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [available, open]);

  if (!available) return null;

  return (
    <span ref={root} className={css.root}>
      <button
        type="button"
        className={css.trigger}
        aria-label={`Context ${percent}% used`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden>
          <circle className={css.track} cx="7" cy="7" r={RADIUS} />
          <circle className={css.fill} cx="7" cy="7" r={RADIUS} strokeDasharray={`${CIRCUMFERENCE * percent / 100} ${CIRCUMFERENCE}`} transform="rotate(-90 7 7)" />
        </svg>
      </button>
      {open && (
        <div className={css.panel} role="dialog" aria-label="Context usage">
          <div className={css.header}>
            <span>Context used</span>
            <span className={css.percent}>{percent}%</span>
            <span className={css.figures}>{formatTokens(usage.context_tokens)} / {formatTokens(contextWindow)}</span>
          </div>
          <div className={css.bar}><i style={{ width: `${percent}%` }} /></div>
          <dl className={css.rows}>
            <div><dt>Cumulative input</dt><dd>{formatTokens(usage.input_tokens)}</dd></div>
            <div><dt>Cache</dt><dd>{formatTokens(cache)}</dd></div>
            <div><dt>Cumulative output</dt><dd>{formatTokens(usage.output_tokens)}</dd></div>
            <div><dt>Requests</dt><dd>{usage.requests}</dd></div>
          </dl>
        </div>
      )}
    </span>
  );
}

function formatTokens(value: number): string {
  if (value < 1_000) return String(value);
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
}
