import { Check, ChevronDown, Clipboard, Terminal, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { CommandResultData } from "../api/types";

export function CommandOutput({ result, onClose }: { result: CommandResultData; onClose: () => void }) {
  const lineCount = result.message ? result.message.split("\n").length : 0;
  const [collapsed, setCollapsed] = useState(lineCount > 16 || result.message.length > 1600);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    setCollapsed(lineCount > 16 || result.message.length > 1600);
    setCopied(false);
  }, [lineCount, result]);
  const copy = async () => {
    await navigator.clipboard?.writeText(result.message);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <section className={`command-output ${result.status}`} aria-label={`/${result.command} result`}>
      <header>
        <button type="button" className="command-output-toggle" onClick={() => setCollapsed((value) => !value)} aria-expanded={!collapsed}>
          <Terminal size={13} />
          <b>/{result.command}</b>
          <span>{result.status}</span>
          {lineCount > 1 && <small>{lineCount} lines</small>}
          <ChevronDown size={13} className={collapsed ? "collapsed" : ""} />
        </button>
        <button type="button" className="icon-button small" title="Copy result" aria-label="Copy command result" onClick={() => void copy()}>
          {copied ? <Check size={13} /> : <Clipboard size={13} />}
        </button>
        <button type="button" className="icon-button small" title="Close result" aria-label="Close command result" onClick={onClose}>
          <X size={13} />
        </button>
      </header>
      {!collapsed && <pre>{result.message}</pre>}
    </section>
  );
}
