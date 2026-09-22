/* Goal bar ported from DeepSeek Harness ui-goal (MIT): an active goal is a
 * banner line naming the objective, with pause / edit / clear next to it.
 * XBot publishes these facts as status slots (goal, goal_objective,
 * goal_round, goal_reason, goal_stats). */
import { Ban, Crosshair, Pencil, Pause } from "lucide-react";
import type { RuntimeSession } from "../state/runtime";

/** Status slot keys written by the goal plugin. */
export const GOAL_SLOTS = {
  status: "goal",
  objective: "goal_objective",
  round: "goal_round",
  reason: "goal_reason",
  stats: "goal_stats",
} as const;

const PHASE_LABELS: Record<string, string> = {
  active: "Ongoing Goal",
  paused: "Goal paused",
  achieved: "Goal achieved",
  failed: "Goal failed",
  cleared: "Goal cleared",
};

export function goalPhaseLabel(status: string | undefined): string | null {
  if (!status) return null;
  return PHASE_LABELS[status] ?? `Goal ${status}`;
}

export function GoalBar({
  current,
  busy,
  onPause,
  onEdit,
  onClear,
}: {
  current: RuntimeSession | null;
  busy?: boolean;
  onPause: () => void;
  onEdit: () => void;
  onClear: () => void;
}) {
  const slots = current?.status_slots ?? {};
  const phase = goalPhaseLabel(slots[GOAL_SLOTS.status]);
  if (phase === null) return null;
  const objective = slots[GOAL_SLOTS.objective] || slots[GOAL_SLOTS.reason] || "";
  const round = slots[GOAL_SLOTS.round];
  const stats = slots[GOAL_SLOTS.stats];
  const active = slots[GOAL_SLOTS.status] === "active";

  return (
    <section className="goal-bar" aria-label="Goal" data-phase={slots[GOAL_SLOTS.status]}>
      <span className="goal-bar-icon" aria-hidden>
        <Crosshair size={15} />
      </span>
      <div className="goal-bar-copy">
        <strong>{phase}</strong>
        {objective && <span className="goal-bar-objective" title={objective}>{objective}</span>}
        <small className="goal-bar-meta">
          {round && <span>Rounds: {round}</span>}
          {stats && <span>{stats}</span>}
          {slots[GOAL_SLOTS.reason] && objective !== slots[GOAL_SLOTS.reason] && (
            <span>{slots[GOAL_SLOTS.reason]}</span>
          )}
        </small>
      </div>
      <div className="goal-bar-actions">
        <button
          type="button"
          className="icon-button"
          title="Pause goal"
          aria-label="Pause goal"
          disabled={!active || busy}
          onClick={onPause}
        >
          <Pause size={14} />
        </button>
        <button
          type="button"
          className="icon-button"
          title="Edit goal"
          aria-label="Edit goal"
          disabled={busy}
          onClick={onEdit}
        >
          <Pencil size={14} />
        </button>
        <button
          type="button"
          className="icon-button"
          title="Clear goal"
          aria-label="Clear goal"
          disabled={busy}
          onClick={onClear}
        >
          <Ban size={14} />
        </button>
      </div>
    </section>
  );
}
