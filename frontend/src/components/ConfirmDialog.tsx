import { useEffect, useRef, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, X } from "lucide-react";

interface Props {
  title: string;
  description: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  confirmDisabled?: boolean;
  children?: ReactNode;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDialog({
  title,
  description,
  confirmLabel,
  danger = false,
  busy = false,
  confirmDisabled = false,
  children,
  onCancel,
  onConfirm,
}: Props) {
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLButtonElement>(".dialog-actions .secondary")?.focus();
    return () => previous?.focus();
  }, []);
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, onCancel]);

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={() => !busy && onCancel()}>
      <section
        className="confirm-dialog"
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-description"
        onMouseDown={event => event.stopPropagation()}
        onKeyDown={event => {
          if (event.key !== "Tab") return;
          const controls = dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)");
          if (!controls?.length) { event.preventDefault(); return; }
          const first = controls[0], last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }}
      >
        <button className="icon-button dialog-close" type="button" aria-label="关闭" onClick={onCancel} disabled={busy}>
          <X size={18} />
        </button>
        <div className={`dialog-symbol ${danger ? "danger-symbol" : "confirm-symbol"}`}>
          {danger ? <AlertTriangle size={22} /> : <CheckCircle2 size={22} />}
        </div>
        <h2 id="confirm-dialog-title">{title}</h2>
        <p id="confirm-dialog-description">{description}</p>
        {children}
        <div className="dialog-actions">
          <button className="secondary" type="button" onClick={onCancel} disabled={busy}>取消</button>
          <button className={danger ? "danger-solid" : ""} type="button" onClick={onConfirm} disabled={busy || confirmDisabled}>
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}
