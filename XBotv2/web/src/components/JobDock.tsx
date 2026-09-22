/* Job list adapted from DeepSeek Harness ui-jobs/JobListAction.tsx (MIT). */
import { ChevronDown, ChevronRight, Circle, Square, XCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { JobData } from "../api/types";

interface JobDockProps {
  jobs: JobData[];
  onStop: (id: string) => Promise<void>;
  onStopAll: () => Promise<void>;
}

/** Ported copy: the list's accessible name and the status words it shows. */
export const JOB_LIST_LABEL = "Background jobs";
export const JOB_STATUS: Record<JobData["status"], string> = {
  pending: "pending",
  running: "running",
  completed: "completed",
  failed: "failed",
  stopped: "stopped",
};

/** A job the registry still holds open, and whose duration therefore ticks. */
export function jobLive(job: JobData): boolean {
  return job.status === "pending" || job.status === "running";
}

/** The trigger's label: how many jobs, and whether any of them is live. */
export function jobCountLabel(jobs: JobData[]): string {
  const live = jobs.filter(jobLive).length;
  const count = live > 0 ? live : jobs.length;
  const noun = count === 1 ? "background job" : "background jobs";
  return live > 0 ? `${count} ${noun} running` : `${count} ${noun}`;
}

/**
 * Elapsed time in at most two adjacent units, as the ported list formats it: a
 * background job that outlives an hour stays in hours.
 */
export function formatJobDuration(elapsedMs: number): string {
  const total = Math.max(0, Math.floor(elapsedMs / 1_000));
  const seconds = total % 60;
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3_600);
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

/** What the row says next to the label: the failure's first line, else status. */
export function jobDetail(job: JobData): string {
  if (job.status === "failed" && job.error) return job.error.split("\n")[0];
  return JOB_STATUS[job.status];
}

/**
 * Live rows first in start order, then settled rows newest-first, so the list
 * never depends on the host's map iteration.
 */
export function orderedJobs(jobs: JobData[]): JobData[] {
  return [...jobs].sort((left, right) => {
    const liveLeft = jobLive(left);
    if (liveLeft !== jobLive(right)) return liveLeft ? -1 : 1;
    if (liveLeft) return left.started_at - right.started_at;
    const finished = (right.finished_at || right.started_at) - (left.finished_at || left.started_at);
    return finished !== 0 ? finished : left.started_at - right.started_at;
  });
}

export function JobDock({ jobs, onStop, onStopAll }: JobDockProps) {
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [outputOpen, setOutputOpen] = useState("");
  const rows = useMemo(() => orderedJobs(jobs), [jobs]);
  const liveCount = jobs.filter(jobLive).length;

  // The clock only runs while an open list is showing something that moves.
  useEffect(() => {
    if (!open || liveCount === 0) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [open, liveCount]);

  // The last job disappearing removes this control; close first so focus does
  // not vanish from an unmounting node.
  useEffect(() => {
    if (jobs.length === 0 && open) setOpen(false);
  }, [jobs.length, open]);

  if (jobs.length === 0) return null;
  const countLabel = jobCountLabel(jobs);

  return (
    <div className="job-menu" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <button
        type="button"
        className="job-menu-trigger"
        aria-label={countLabel}
        aria-expanded={open}
        onClick={() => {
          setNow(Date.now());
          setOpen((value) => !value);
        }}
      >
        {liveCount > 0 && <Circle className="job-menu-live" size={8} fill="currentColor" />}
        <span>{countLabel}</span>
        <ChevronDown size={13} className={open ? "job-menu-chevron open" : "job-menu-chevron"} />
      </button>
      {open && (
        <section className="job-menu-popover">
          {liveCount > 1 && (
            <header>
              <strong>{JOB_LIST_LABEL}</strong>
              <button className="icon-button small" title="Stop all jobs" aria-label="Stop all jobs" onClick={() => void onStopAll()}>
                <XCircle size={14} />
              </button>
            </header>
          )}
          <ul className="job-menu-list" aria-label={JOB_LIST_LABEL}>
            {rows.map((job) => {
              const live = jobLive(job);
              const elapsed = live ? now - job.started_at : (job.finished_at || job.started_at) - job.started_at;
              const duration = formatJobDuration(elapsed);
              const label = job.command || job.agent;
              return (
                <li className={`job-item job-${job.status}`} key={job.job_id}>
                  <span className="job-row">
                    <Circle size={8} fill="currentColor" className={`job-dot job-dot-${job.status}`} />
                    <span className="job-kind">{job.kind}</span>
                    <span className="job-label" title={label}>{label}</span>
                    <span className="job-state" title={job.error || undefined}>{jobDetail(job)}</span>
                    <span
                      className="job-duration"
                      title={live ? `Running for ${duration}` : `Took ${duration}`}
                    >
                      {duration}
                    </span>
                    {live && (
                      <button className="icon-button small" title="Stop job" aria-label={`Stop ${job.job_id}`} onClick={() => void onStop(job.job_id)}>
                        <Square size={11} fill="currentColor" />
                      </button>
                    )}
                    {(job.output || job.error) && (
                      <button
                        className="icon-button small"
                        title="Show output"
                        aria-label={`Show output of ${job.job_id}`}
                        aria-expanded={outputOpen === job.job_id}
                        onClick={() => setOutputOpen((current) => current === job.job_id ? "" : job.job_id)}
                      >
                        <ChevronRight size={13} className={outputOpen === job.job_id ? "open" : ""} />
                      </button>
                    )}
                  </span>
                  {outputOpen === job.job_id && (
                    <div className="job-output" tabIndex={0}>
                      {job.thread_id && <div className="job-meta">thread: {job.thread_id}</div>}
                      <pre>{job.error || job.output || job.command}</pre>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
