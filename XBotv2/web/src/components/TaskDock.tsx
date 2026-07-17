import { Bot, ChevronRight, Square, Terminal, XCircle } from "lucide-react";
import type { TaskData } from "../api/types";

interface TaskDockProps {
  tasks: TaskData[];
  onStop: (id: string) => Promise<void>;
  onStopAll: () => Promise<void>;
}

export function TaskDock({ tasks, onStop, onStopAll }: TaskDockProps) {
  if (!tasks.length) return null;
  const active = tasks.filter((task) => task.status === "pending" || task.status === "running");
  return (
    <section className="task-dock" aria-label="Background tasks">
      <div className="task-dock-heading">
        <span>Tasks <b>{active.length}</b></span>
        {active.length > 1 && (
          <button className="icon-button small" title="Stop all tasks" aria-label="Stop all tasks" onClick={() => void onStopAll()}>
            <XCircle size={14} />
          </button>
        )}
      </div>
      <div className="task-list-web">
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
  );
}
