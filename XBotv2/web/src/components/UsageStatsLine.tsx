/* Adapted from DeepSeek Harness StatsLine.tsx (MIT). */
import type { SessionStatsData, UsageData } from "../api/types";
import css from "./UsageStatsLine.module.css";

const EMPTY_STATS: SessionStatsData = {
  turns: 0,
  steps: 0,
  llm_ms: 0,
  tool_ms: 0,
  ttft_ms: 0,
  ttft_steps: 0,
  decode_ms: 0,
  decode_tokens: 0,
};

export function UsageStatsLine({ usage, stats = EMPTY_STATS }: {
  usage: UsageData;
  stats?: SessionStatsData;
}) {
  const groups: string[] = [];
  if (stats.steps > 0) {
    groups.push(`${stats.turns} turns · ${stats.steps} steps`);
    const durations = [
      stats.llm_ms > 0 ? `LLM ${formatDuration(stats.llm_ms)}` : "",
      stats.tool_ms > 0 ? `Tool ${formatDuration(stats.tool_ms)}` : "",
    ].filter(Boolean);
    if (durations.length) groups.push(durations.join(" · "));
    const speed = [
      stats.ttft_steps > 0 ? `TTFT ${formatDuration(stats.ttft_ms / stats.ttft_steps)} avg` : "",
      stats.decode_ms > 0 ? `${formatRate(stats.decode_tokens / (stats.decode_ms / 1_000))} tok/s` : "",
    ].filter(Boolean);
    if (speed.length) groups.push(speed.join(" · "));
  }
  const input = billedInput(usage);
  if (input > 0 || usage.output_tokens > 0) {
    if (input > 0) groups.push(`Cache hit ${Math.round(usage.cache_read_input_tokens / input * 100)}%`);
    groups.push(`Input ${formatTokens(input)} · Output ${formatTokens(usage.output_tokens)}`);
  }
  if (!groups.length) return null;
  const line = groups.join(" | ");
  return <div className={css.root} role="status" aria-label="Session usage" title={line}>{line}</div>;
}

export function billedInput(usage: UsageData): number {
  return usage.input_tokens
    + usage.cache_read_input_tokens
    + usage.cache_creation_input_tokens
    + usage.prompt_cache_write_tokens;
}

export function formatTokens(value: number): string {
  const scaled = (number: number) => number >= 100
    ? String(Math.round(number))
    : String(Math.round(number * 10) / 10);
  if (value < 1_000) return String(value);
  if (value < 1_000_000) return `${scaled(value / 1_000)}K`;
  return `${scaled(value / 1_000_000)}M`;
}

export function formatDuration(ms: number): string {
  const seconds = ms / 1_000;
  if (seconds < 60) return `${Math.round(seconds * 10) / 10}s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}m${whole % 60}s`;
}

function formatRate(value: number): string {
  return value >= 100 ? String(Math.round(value)) : String(Math.round(value * 10) / 10);
}
