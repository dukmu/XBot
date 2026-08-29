import { Check, Circle, CircleDot } from "lucide-react";
import type { TodoItemData } from "../api/types";


export function TodoDock({ items }: { items: TodoItemData[] }) {
  if (!items.length) return null;
  return (
    <details className="todo-dock" open>
      <summary>Plan <span>{items.filter((item) => item.status === "completed").length}/{items.length}</span></summary>
      <div>
        {items.map((item, index) => (
          <div className={`todo-item todo-${item.status}`} key={`${index}-${item.content}`}>
            {item.status === "completed" ? <Check size={14} /> : item.status === "in_progress" ? <CircleDot size={14} /> : <Circle size={14} />}
            <span>{item.content}</span>
            <small>{item.status === "completed" ? "Done" : item.status === "in_progress" ? "In progress" : "Pending"}</small>
          </div>
        ))}
      </div>
    </details>
  );
}
