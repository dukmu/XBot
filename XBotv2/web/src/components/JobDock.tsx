import { Bot, ChevronDown, ChevronRight, Circle, Square, Terminal, XCircle } from "lucide-react";
import { useState } from "react";
import type { JobData } from "../api/types";

interface JobDockProps {
  jobs: JobData[];
  onStop: (id: string) => Promise<void>;
  onStopAll: () => Promise<void>;
}

export function JobDock({ jobs, onStop, onStopAll }: JobDockProps) {
  const [open, setOpen] = useState(false);
  if (!jobs.length) return null;
  const active = jobs.filter((job) => job.status === "pending" || job.status === "running");
  return (
    <div className="job-menu" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <button
        type="button"
        className="job-menu-trigger"
        aria-label="Background jobs"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Circle className={active.length ? "job-menu-live" : ""} size={8} fill="currentColor" />
        <span>{jobs.length} job{jobs.length === 1 ? "" : "s"}</span>
        <ChevronDown size={13} className={open ? "job-menu-chevron open" : "job-menu-chevron"} />
      </button>
      {open && (
        <section className="job-menu-popover" aria-label="Background jobs list">
          <header>
            <strong>Background jobs</strong>
            {active.length > 1 && (
              <button className="icon-button small" title="Stop all jobs" aria-label="Stop all jobs" onClick={() => void onStopAll()}>
                <XCircle size={14} />
              </button>
            )}
          </header>
          <div className="job-menu-list">
            {jobs.map((job) => (
              <details className={`job-item job-${job.status}`} key={job.job_id} open={job.kind === "agent" && job.status === "failed"}>
                <summary>
                  {job.kind === "agent" ? <Bot size={14} /> : <Terminal size={14} />}
                  <span className="job-label">{job.agent || job.command}</span>
                  <span className="job-state">{job.status}</span>
                  {(job.status === "pending" || job.status === "running") && (
                    <button className="icon-button small" title="Stop job" aria-label={`Stop ${job.job_id}`} onClick={(event) => {
                      event.preventDefault();
                      void onStop(job.job_id);
                    }}>
                      <Square size={11} fill="currentColor" />
                    </button>
                  )}
                  <ChevronRight size={13} className="summary-chevron" />
                </summary>
                <div className="job-output" tabIndex={0}>
                  {job.thread_id && <div className="job-meta">thread: {job.thread_id}</div>}
                  <pre>{job.error || job.output || job.command}</pre>
                </div>
              </details>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
