import { Bot, ChevronDown, ChevronRight, Circle, Square, Terminal, XCircle } from "lucide-react";
import { useState } from "react";
import type { TaskData } from "../api/types";

interface TaskDockProps {
  tasks: TaskData[];
  onStop: (id: string) => Promise<void>;
  onStopAll: () => Promise<void>;
}

export function TaskDock({ tasks, onStop, onStopAll }: TaskDockProps) {
  const [open, setOpen] = useState(false);
  if (!tasks.length) return null;
  const active = tasks.filter((task) => task.status === "pending" || task.status === "running");
  return (
    <div className="task-menu" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <button
        type="button"
        className="task-menu-trigger"
        aria-label="Background tasks"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Circle className={active.length ? "task-menu-live" : ""} size={8} fill="currentColor" />
        <span>{tasks.length} task{tasks.length === 1 ? "" : "s"}</span>
        <ChevronDown size={13} className={open ? "task-menu-chevron open" : "task-menu-chevron"} />
      </button>
      {open && (
        <section className="task-menu-popover" aria-label="Background tasks list">
          <header>
            <strong>Background tasks</strong>
            {active.length > 1 && (
              <button className="icon-button small" title="Stop all tasks" aria-label="Stop all tasks" onClick={() => void onStopAll()}>
                <XCircle size={14} />
              </button>
            )}
          </header>
          <div className="task-menu-list">
            {tasks.map((task) => (
              <details className={`task-item task-${task.status}`} key={task.task_id} open={task.kind === "agent" && task.status === "failed"}>
                <summary>
                  {task.kind === "agent" ? <Bot size={14} /> : <Terminal size={14} />}
                  <span className="task-label">{task.agent || task.command}</span>
                  <span className="task-state">{task.status}</span>
                  {(task.status === "pending" || task.status === "running") && (
                    <button className="icon-button small" title="Stop task" aria-label={`Stop ${task.task_id}`} onClick={(event) => {
                      event.preventDefault();
                      void onStop(task.task_id);
                    }}>
                      <Square size={11} fill="currentColor" />
                    </button>
                  )}
                  <ChevronRight size={13} className="summary-chevron" />
                </summary>
                <div className="task-output" tabIndex={0}>
                  {task.thread_id && <div className="task-meta">thread: {task.thread_id}</div>}
                  <pre>{task.error || task.output || task.command}</pre>
                </div>
              </details>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
