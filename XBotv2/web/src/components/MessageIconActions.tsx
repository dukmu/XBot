/* Shared message action strip adapted from DeepSeek Harness (MIT). */
import { Check, Clipboard, GitBranch, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import styles from "./MessageIconActions.module.css";

export function MessageIconActions({
  text,
  onRegenerate,
  onBranch,
  branchUnavailable = false,
  className,
}: {
  text: string;
  onRegenerate?: () => Promise<void>;
  onBranch?: () => Promise<void>;
  branchUnavailable?: boolean;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const copyPending = useRef(false);
  const timer = useRef<number | null>(null);
  const epoch = useRef(0);
  const branchReasonId = useId();

  useEffect(() => () => {
    epoch.current += 1;
    copyPending.current = false;
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);

  const copy = useCallback(async () => {
    if (copied || copyPending.current || !navigator.clipboard) return;
    const currentEpoch = epoch.current;
    copyPending.current = true;
    try {
      await navigator.clipboard.writeText(text);
      if (currentEpoch !== epoch.current) return;
      setCopied(true);
      timer.current = window.setTimeout(() => {
        timer.current = null;
        setCopied(false);
      }, 1000);
    } catch {
      // Browser clipboard denial leaves the action available for another try.
    } finally {
      if (currentEpoch === epoch.current) copyPending.current = false;
    }
  }, [copied, text]);

  return (
    <div className={className ? `${styles.actions} ${className}` : styles.actions}>
      <button
        type="button"
        className={styles.action}
        aria-label={copied ? "Copied" : "Copy"}
        title={copied ? "Copied" : "Copy"}
        onClick={() => void copy()}
      >
        {copied ? <Check size={16} /> : <Clipboard size={16} />}
      </button>
      {onRegenerate && (
        <button
          type="button"
          className={styles.action}
          aria-label="Regenerate response"
          title="Regenerate response"
          onClick={() => void onRegenerate()}
        >
          <RotateCcw size={16} />
        </button>
      )}
      {onBranch && (
        <button
          type="button"
          className={styles.action}
          aria-label="Branch into a new conversation"
          aria-disabled={branchUnavailable || undefined}
          aria-describedby={branchUnavailable ? branchReasonId : undefined}
          data-unavailable={branchUnavailable || undefined}
          title={branchUnavailable
            ? "Available only on the last message of a completed turn"
            : "Branch into a new conversation"}
          onClick={branchUnavailable ? undefined : () => void onBranch()}
        >
          <GitBranch size={16} />
        </button>
      )}
      {onBranch && branchUnavailable && (
        <span id={branchReasonId} className={styles.visuallyHidden}>
          Available only on the last message of a completed turn
        </span>
      )}
    </div>
  );
}
