/* Adapted from DeepSeek Harness ImageLightbox.tsx (MIT). */
import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import css from "./ImageLightbox.module.css";

export function ImageLightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  const restore = useRef<HTMLElement | null>(null);
  useEffect(() => {
    restore.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    close.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      restore.current?.focus();
    };
  }, [onClose]);
  return createPortal(
    <div className={css.backdrop} role="dialog" aria-modal="true" aria-label={`Preview ${alt}`}>
      <div className={css.mask} aria-hidden onMouseDown={onClose} />
      <img className={css.image} src={src} alt={alt} />
      <button ref={close} type="button" className={css.close} aria-label="Close image preview" onClick={onClose}><X size={18} /></button>
    </div>,
    document.body,
  );
}
