/* Adapted from DeepSeek Harness DropOverlay.tsx (MIT). */
import { Upload } from "lucide-react";
import { createPortal } from "react-dom";
import css from "./DropOverlay.module.css";

export function DropOverlay({ disabled }: { disabled: boolean }) {
  return createPortal(
    <div className={css.mask} role="status">
      <div className={css.wrap}>
        <div className={css.icon}><Upload size={34} /></div>
        <div className={css.title}>{disabled ? "Attachments are unavailable" : "Drop files to attach"}</div>
        {!disabled && <div className={css.desc}>Release anywhere in this window</div>}
      </div>
    </div>,
    document.body,
  );
}
