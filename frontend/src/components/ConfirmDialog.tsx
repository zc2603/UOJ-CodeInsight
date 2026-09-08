import { useEffect } from "react";
import { AlertTriangle, X } from "lucide-react";

interface Props {
  title: string;
  description: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDialog({
  title,
  description,
  confirmLabel,
  danger = false,
  busy = false,
  onCancel,
  onConfirm,
}: Props) {
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
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        onMouseDown={event => event.stopPropagation()}
      >
        <button className="icon-button dialog-close" type="button" aria-label="关闭" onClick={onCancel} disabled={busy}>
          <X size={18} />
        </button>
        <div className={`dialog-symbol ${danger ? "danger-symbol" : "warning-symbol"}`}>
          <AlertTriangle size={22} />
        </div>
        <h2 id="confirm-dialog-title">{title}</h2>
        <p>{description}</p>
        <div className="dialog-actions">
          <button className="secondary" type="button" onClick={onCancel} disabled={busy}>取消</button>
          <button className={danger ? "danger-solid" : ""} type="button" onClick={onConfirm} disabled={busy}>
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}
