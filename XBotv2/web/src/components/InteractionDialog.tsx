import { Check, ShieldAlert, X } from "lucide-react";
import { useState } from "react";
import type { InteractionRequest } from "../api/types";

interface InteractionDialogProps {
  request: InteractionRequest;
  pendingCount: number;
  onResolve: (
    request: InteractionRequest,
    answer: unknown,
    scope?: "once" | "session",
  ) => Promise<void>;
}

export function InteractionDialog({ request, pendingCount, onResolve }: InteractionDialogProps) {
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (value: unknown, scope: "once" | "session" = "once") => {
    setSubmitting(true);
    try {
      await onResolve(request, value, scope);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="dialog-backdrop interaction-backdrop">
      <section className="dialog interaction-dialog" role="dialog" aria-modal="true" aria-labelledby="interaction-title">
        <div className="dialog-heading">
          <div className="interaction-title-row">
            <span className="interaction-icon"><ShieldAlert size={18} /></span>
            <div>
              <span className="eyebrow">Request {pendingCount > 1 ? `1 of ${pendingCount}` : ""}</span>
              <h2 id="interaction-title">{request.kind === "permission" ? "Approval required" : request.question}</h2>
            </div>
          </div>
        </div>

        {request.kind === "permission" ? (
          <>
            <div className="permission-tool">
              <strong>{request.tool_call.name}</strong>
              <pre>{JSON.stringify(request.tool_call.args, null, 2)}</pre>
            </div>
            {request.reason && <p className="interaction-reason">{request.reason}</p>}
            <div className="dialog-actions permission-actions">
              <button disabled={submitting} className="secondary-button danger" onClick={() => void submit("deny")}>
                <X size={15} /> Deny
              </button>
              <button disabled={submitting} className="secondary-button" onClick={() => void submit("allow")}>
                <Check size={15} /> Allow once
              </button>
              <button disabled={submitting} className="primary-button" onClick={() => void submit("allow", "session")}>
                <Check size={15} /> Allow session
              </button>
            </div>
          </>
        ) : request.options.length > 0 ? (
          <div className="choice-list">
            {request.options.map((option) => (
              <button key={option.label} disabled={submitting} onClick={() => void submit(option.label)}>
                <strong>{option.label}</strong>
                <span>{option.description}</span>
              </button>
            ))}
          </div>
        ) : (
          <form onSubmit={(event) => {
            event.preventDefault();
            if (answer.trim()) void submit(answer.trim());
          }}>
            <textarea autoFocus value={answer} onChange={(event) => setAnswer(event.target.value)} />
            <div className="dialog-actions">
              <button className="primary-button" disabled={submitting || !answer.trim()} type="submit">Submit</button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
