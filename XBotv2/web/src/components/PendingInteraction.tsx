/* Inline interaction card adapted from DeepSeek Harness ui-conversation chat rows (MIT). */
import { useEffect, useState } from "react";
import type { InteractionRequest } from "../api/types";

/** Ported copy: the approval group's name and its two decisions. */
export const APPROVAL_DETAILS = "Approval details";
export const REJECT = "Reject";
export const ALLOW_ONCE = "Allow once";
/** XBot can remember an approval for the whole session; the ported card cannot. */
export const ALLOW_SESSION = "Allow session";
export const SUBMIT = "Submit";
export const ANSWER_PLACEHOLDER = "Type your answer";

/**
 * The pending interaction, rendered in the transcript where dsh keeps it rather
 * than as a modal: an approval is a labelled `Approval details` group above its
 * decisions, and a question is a region named by the question itself.
 *
 * XBot's request carries one question with options, so the batch chrome the
 * ported card has for several questions (paging, dismiss-all, skip) has nothing
 * to act on here and is deliberately absent.
 */
export function PendingInteraction({
  request,
  onResolve,
}: {
  request: InteractionRequest;
  onResolve: (
    request: InteractionRequest,
    answer: unknown,
    scope?: "once" | "session",
  ) => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const [option, setOption] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    setAnswer("");
    setOption("");
    setSubmitting(false);
  }, [request.request_id]);

  const submit = async (value: unknown, scope: "once" | "session" = "once") => {
    setSubmitting(true);
    try {
      await onResolve(request, value, scope);
    } finally {
      setSubmitting(false);
    }
  };

  if (request.kind === "permission") {
    return (
      <div className="pending-interaction pending-approval" role="group" aria-label={APPROVAL_DETAILS}>
        <div className="permission-tool">
          <strong>{request.tool_call.name}</strong>
          <pre>{JSON.stringify(request.tool_call.args, null, 2)}</pre>
        </div>
        {request.reason && <p className="interaction-reason">{request.reason}</p>}
        <div className="interaction-actions">
          <button
            type="button"
            className="secondary-button danger"
            disabled={submitting}
            onClick={() => void submit("deny")}
          >
            {REJECT}
          </button>
          <button
            type="button"
            autoFocus
            className="secondary-button"
            disabled={submitting}
            onClick={() => void submit("allow")}
          >
            {ALLOW_ONCE}
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={submitting}
            onClick={() => void submit("allow", "session")}
          >
            {ALLOW_SESSION}
          </button>
        </div>
      </div>
    );
  }

  const chosen = option || answer.trim();
  return (
    <section className="pending-interaction pending-question" role="region" aria-label={request.question}>
      <h2>{request.question}</h2>
      {/* The ported card's option group is unnamed; the region above carries the question. */}
      {request.options.length > 0 && (
        <div className="question-options" role="group">
          {request.options.map((choice, index) => (
            <label className="question-option" key={choice.label}>
              <input
                type="radio"
                name={`question:${request.request_id}`}
                autoFocus={index === 0}
                checked={option === choice.label}
                disabled={submitting}
                onChange={() => {
                  setOption(choice.label);
                  setAnswer("");
                }}
              />
              <span>
                <strong>{choice.label}</strong>
                <small>{choice.description}</small>
              </span>
            </label>
          ))}
        </div>
      )}
      <textarea
        className="question-answer"
        aria-label={ANSWER_PLACEHOLDER}
        placeholder={ANSWER_PLACEHOLDER}
        value={answer}
        disabled={submitting}
        onChange={(event) => {
          setAnswer(event.target.value);
          setOption("");
        }}
      />
      <div className="interaction-actions">
        <button
          type="button"
          className="primary-button"
          disabled={submitting || !chosen}
          onClick={() => void submit(chosen)}
        >
          {SUBMIT}
        </button>
      </div>
    </section>
  );
}
