/** Small presentational primitives shared by the pages. */

import { useEffect, useRef, useState } from "react";

export function Card({ title, description, actions, children }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div>
            {title && <h2 className="text-base font-semibold text-slate-900">{title}</h2>}
            {description && <p className="mt-1 text-sm text-slate-500">{description}</p>}
          </div>
          {actions}
        </header>
      )}
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

export function Button({ variant = "primary", type = "button", className = "", ...props }) {
  const styles = {
    primary: "bg-slate-900 text-white hover:bg-slate-700 disabled:bg-slate-400",
    secondary:
      "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:text-slate-400",
    danger: "border border-red-200 bg-white text-red-600 hover:bg-red-50 disabled:text-red-300",
  }[variant];

  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center rounded-md px-3 py-2 text-sm font-medium transition disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-500 focus-visible:ring-offset-2 ${styles} ${className}`}
      {...props}
    />
  );
}

export function Field({ label, hint, error, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-slate-700">{label}</span>
      {children}
      {hint && !error && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
      {error && (
        <span className="mt-1 block text-xs text-red-600" role="alert">
          {error}
        </span>
      )}
    </label>
  );
}

export function TextInput({ className = "", ...props }) {
  return (
    <input
      className={`w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500 ${className}`}
      {...props}
    />
  );
}

export function Select({ className = "", children, ...props }) {
  return (
    <select
      className={`w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500 ${className}`}
      {...props}
    >
      {children}
    </select>
  );
}

export function Checkbox({ label, ...props }) {
  return (
    <label className="flex items-center gap-2 text-sm text-slate-700">
      <input type="checkbox" className="h-4 w-4 rounded border-slate-300" {...props} />
      {label}
    </label>
  );
}

/** Non-blocking error banner. Announced to assistive tech via role=alert. */
export function ErrorBanner({ error, onDismiss }) {
  if (!error) return null;
  const code = error.code && error.code !== "APP_ERROR" ? error.code : null;
  return (
    <div
      role="alert"
      className="flex items-start justify-between gap-3 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
    >
      <div>
        <p className="font-medium">{error.message || "Не удалось выполнить операцию"}</p>
        {code && <p className="mt-0.5 text-xs text-red-600">{code}</p>}
      </div>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs font-medium text-red-700 underline"
          aria-label="Закрыть сообщение об ошибке"
        >
          Закрыть
        </button>
      )}
    </div>
  );
}

export function Badge({ tone = "neutral", children }) {
  const tones = {
    neutral: "bg-slate-100 text-slate-700",
    success: "bg-emerald-100 text-emerald-800",
    warning: "bg-amber-100 text-amber-800",
    danger: "bg-red-100 text-red-700",
  };
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function EmptyState({ title, description }) {
  return (
    <div className="rounded-md border border-dashed border-slate-300 px-5 py-10 text-center">
      <p className="text-sm font-medium text-slate-700">{title}</p>
      {description && <p className="mt-1 text-sm text-slate-500">{description}</p>}
    </div>
  );
}

/**
 * Compact icon-only button.
 *
 * Used where a text button would bloat a table row. The label is mandatory: it
 * becomes both `aria-label` and `title`, so the control stays reachable by
 * screen readers and explains itself on hover.
 */
export function IconButton({ label, active = false, className = "", children, ...props }) {
  const tone = active
    ? "border-slate-400 bg-slate-100 text-slate-900"
    : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50";

  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={`inline-flex h-8 w-8 items-center justify-center rounded-md border transition disabled:cursor-not-allowed disabled:text-slate-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-500 focus-visible:ring-offset-1 ${tone} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

/** Triangular play glyph. Inline SVG keeps the bundle free of an icon library. */
export function PlayIcon({ className = "h-4 w-4" }) {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M6.3 3.6a1 1 0 0 1 1.02.05l8 5.5a1 1 0 0 1 0 1.7l-8 5.5A1 1 0 0 1 5.75 15.5V4.5a1 1 0 0 1 .55-.9Z" />
    </svg>
  );
}

/** Two vertical bars, the paused counterpart of PlayIcon. */
export function StopIcon({ className = "h-4 w-4" }) {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M6.5 4h2.2v12H6.5V4Zm4.8 0h2.2v12h-2.2V4Z" />
    </svg>
  );
}

/** Chevron that rotates to signal expanded state. */
export function ChevronIcon({ expanded = false, className = "h-4 w-4" }) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="currentColor"
      aria-hidden="true"
      className={`${className} transition-transform ${expanded ? "rotate-180" : ""}`}
    >
      <path d="M5.6 7.5a1 1 0 0 1 1.4-.1L10 10.2l3-2.8a1 1 0 0 1 1.4 1.5l-3.7 3.4a1 1 0 0 1-1.36 0L5.7 8.9a1 1 0 0 1-.1-1.4Z" />
    </svg>
  );
}

/**
 * Dropdown panel anchored to a trigger button.
 *
 * Closes on outside click and on Escape so it behaves like a native menu. Used
 * for the queue column picker.
 */
export function Popover({ label, children, align = "right" }) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;

    const onPointerDown = (event) => {
      if (!containerRef.current?.contains(event.target)) setOpen(false);
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") setOpen(false);
    };

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative inline-block">
      <Button
        variant="secondary"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-haspopup="true"
      >
        {label} <ChevronIcon expanded={open} className="ml-1 h-4 w-4" />
      </Button>

      {open && (
        <div
          className={`absolute z-20 mt-1 w-64 rounded-md border border-slate-200 bg-white p-3 shadow-lg ${
            align === "right" ? "right-0" : "left-0"
          }`}
        >
          {children}
        </div>
      )}
    </div>
  );
}

