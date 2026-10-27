/* Job list adapted from DeepSeek Harness ui-jobs/JobListAction.tsx (MIT). */
import { ChevronDown, Circle, Square, XCircle } from "lucide-react";
import { useState } from "react";
import type { JobData } from "../api/types";

interface JobDockProps {
  jobs: JobData[];
  onStop: (id: string) => Promise<void>;
  onStopAll: () => Promise<void>;
}

export const JOB_LIST_LABEL = "Background jobs";
export function jobLive(job: JobData): boolean {
  return job.state === "queued" || job.state === "running";
}
export function jobCountLabel(jobs: JobData[]): string {
  const live = jobs.filter(jobLive).length;
  const count = live || jobs.length;
  return `${count} background ${count === 1 ? "job" : "jobs"}${live ? " running" : ""}`;
}
export function formatJobDuration(elapsedMs: number): string {
  const total = Math.max(0, Math.floor(elapsedMs / 1000));
  const seconds = total % 60;
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}
export function JobDock({ jobs, onStop, onStopAll }: JobDockProps) {
  const [open, setOpen] = useState(false);
  if (!jobs.length) return null;
  const liveCount = jobs.filter(jobLive).length;
  return <div className="job-menu" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <button type="button" className="job-menu-trigger" aria-label={jobCountLabel(jobs)} aria-expanded={open} onClick={() => setOpen(!open)}>
      {liveCount > 0 && <Circle className="job-menu-live" size={8} fill="currentColor" />}
      <span>{jobCountLabel(jobs)}</span><ChevronDown size={13} />
    </button>
    {open && <section className="job-menu-popover">
      {liveCount > 1 && <header><strong>{JOB_LIST_LABEL}</strong><button className="icon-button small" aria-label="Stop all jobs" onClick={() => void onStopAll()}><XCircle size={14} /></button></header>}
      <ul className="job-menu-list" aria-label={JOB_LIST_LABEL}>
        {jobs.map((job) => <li className={`job-item job-${job.state}`} key={job.id}>
          <span className="job-row"><Circle size={8} fill="currentColor" className={`job-dot job-dot-${job.state}`} />
            <span className="job-kind">{job.kind}</span><span className="job-label" title={job.label}>{job.label}</span>
            <span className="job-state" title={job.summary ?? undefined}>{job.summary || job.state}</span>
            <span className="job-duration">{formatJobDuration(job.elapsed_ms)}</span>
            {jobLive(job) && <button className="icon-button small" aria-label={`Stop ${job.id}`} onClick={() => void onStop(job.id)}><Square size={11} fill="currentColor" /></button>}
          </span>
        </li>)}
      </ul>
    </section>}
  </div>;
}
