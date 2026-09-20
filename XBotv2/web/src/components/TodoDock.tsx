import { Check, Circle, CircleDot } from "lucide-react";
import type { TodoItemData } from "../api/types";


export function TodoDock({ items }: { items: TodoItemData[] }) {
  if (!items.length) return null;
  return (
    <details className="todo-dock" open>
      <summary>Tasks <span>{items.filter((item) => item.status === "completed").length}/{items.length}</span></summary>
      <div>
        {items.map((item) => (
          <div className={`todo-item todo-${item.status}`} key={item.id || item.subject}>
            {item.status === "completed" ? <Check size={14} /> : item.status === "in_progress" ? <CircleDot size={14} /> : <Circle size={14} />}
            <span>{item.subject}</span>
            <small>{taskMeta(item)}</small>
          </div>
        ))}
      </div>
    </details>
  );
}

function taskMeta(item: TodoItemData): string {
  if (item.status === "completed") return "Done";
  const parts: string[] = [];
  if (item.status === "in_progress") parts.push(item.activeForm || "In progress");
  else parts.push("Pending");
  if (item.owner) parts.push(`owner: ${item.owner}`);
  if (item.blockedBy.length) parts.push(`blocked by #${item.blockedBy.join(", #")}`);
  return parts.join(" · ");
}
