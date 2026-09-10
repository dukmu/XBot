import type { CommandInfo } from "../api/types";
import styles from "./CommandTriggerMenu.module.css";

export function CommandTriggerMenu({
  commands,
  selectedIndex,
  onPick,
}: {
  commands: CommandInfo[];
  selectedIndex: number;
  onPick: (command: CommandInfo) => void;
}) {
  if (!commands.length) return null;
  return (
    <div className={styles.menu} role="listbox" aria-label="Commands">
      {commands.map((command, index) => (
        <button
          type="button"
          role="option"
          aria-selected={index === selectedIndex}
          className={`${styles.item} ${index === selectedIndex ? styles.selected : ""}`}
          key={`${command.kind}:${command.name}`}
          onMouseDown={(event) => {
            event.preventDefault();
            onPick(command);
          }}
        >
          <span className={styles.name}><b>{command.slash}</b><small>{command.kind}</small></span>
          <span className={styles.description}>{command.description}</span>
          <code className={styles.usage}>{command.usage}</code>
        </button>
      ))}
    </div>
  );
}
