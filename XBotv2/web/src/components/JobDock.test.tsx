import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { JobData } from "../api/types";
import {
  JOB_LIST_LABEL,
  JobDock,
  formatJobDuration,
  jobCountLabel,
  jobDetail,
  orderedJobs,
} from "./JobDock";

function job(overrides: Partial<JobData> = {}): JobData {
  return {
    job_id: "job-1",
    kind: "shell",
    command: "sleep 45",
    cwd: "/workspace",
    status: "running",
    created_at: 0,
    started_at: 0,
    finished_at: 0,
    output: "",
    error: "",
    agent: "",
    thread_id: "",
    usage: {},
    ...overrides,
  };
}

/**
 * The ported `background-job-list` scenario: one `list "Background jobs"` whose
 * items read `kind label status duration`, with the duration of a live row
 * ticking while the list is open.
 */
describe("JobDock", () => {
  it("labels the trigger with the live count and lists rows as one list", () => {
    render(<JobDock jobs={[job()]} onStop={vi.fn()} onStopAll={vi.fn()} />);
    expect(screen.getByRole("button", { name: "1 background job running" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "1 background job running" }));
    const list = screen.getByRole("list", { name: JOB_LIST_LABEL });
    const [row] = screen.getAllByRole("listitem");
    expect(list.contains(row)).toBe(true);
    expect(row.textContent).toContain("shell");
    expect(row.textContent).toContain("sleep 45");
    expect(row.textContent).toContain("running");
  });

  it("shows a settled job with its status and elapsed time", () => {
    render(
      <JobDock
        jobs={[job({ status: "stopped", started_at: 1_000, finished_at: 46_000 })]}
        onStop={vi.fn()}
        onStopAll={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "1 background job" }));
    const [row] = screen.getAllByRole("listitem");
    expect(row.textContent).toContain("stopped");
    expect(row.textContent).toContain("45s");
    // A settled row offers no stop control.
    expect(screen.queryByRole("button", { name: "Stop job-1" })).toBeNull();
  });

  it("stops a live job from its row and expands its output on demand", () => {
    const onStop = vi.fn().mockResolvedValue(undefined);
    render(<JobDock jobs={[job({ output: "partial output" })]} onStop={onStop} onStopAll={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "1 background job running" }));
    expect(screen.queryByText("partial output")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show output of job-1" }));
    expect(screen.getByText("partial output")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop job-1" }));
    expect(onStop).toHaveBeenCalledWith("job-1");
  });
});

describe("job list projection", () => {
  it("formats elapsed time in at most two adjacent units", () => {
    expect(formatJobDuration(45_000)).toBe("45s");
    expect(formatJobDuration(125_000)).toBe("2m 5s");
    expect(formatJobDuration(3_725_000)).toBe("1h 2m");
  });

  it("counts only live jobs in the running label", () => {
    expect(jobCountLabel([job(), job({ job_id: "b" })])).toBe("2 background jobs running");
    expect(jobCountLabel([job({ status: "completed" })])).toBe("1 background job");
    expect(jobCountLabel([job({ status: "completed" }), job({ job_id: "b", status: "stopped" })]))
      .toBe("2 background jobs");
  });

  it("orders live rows by start and settled rows newest-first", () => {
    const rows = orderedJobs([
      job({ job_id: "settled-old", status: "completed", started_at: 0, finished_at: 10 }),
      job({ job_id: "live-late", started_at: 20 }),
      job({ job_id: "settled-new", status: "stopped", started_at: 0, finished_at: 30 }),
      job({ job_id: "live-early", started_at: 5 }),
    ]);
    expect(rows.map((row) => row.job_id)).toEqual(["live-early", "live-late", "settled-new", "settled-old"]);
  });

  it("names the failure in the status slot", () => {
    expect(jobDetail(job({ status: "failed", error: "boom\nstack" }))).toBe("boom");
    expect(jobDetail(job({ status: "completed" }))).toBe("completed");
  });
});
