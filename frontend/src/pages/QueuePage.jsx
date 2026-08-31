import { useCallback, useEffect, useMemo, useState } from "react";

import { queueApi, connectionsApi } from "../api/endpoints";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  ErrorBanner,
  Popover,
} from "../components/ui";
import { useEvents } from "../realtime/EventsContext";
import { Link } from "../router";
import { uiKey, usePersistedState } from "../state/storage";

/**
 * Transcription queue management.
 *
 * Shows every item the current user owns across all connections. Actions:
 *   - per-item cancel (queued only), retry (failed/skipped/canceled)
 *   - per-item delete (anything except PROCESSING)
 *   - bulk retry (all failed/skipped)
 *   - bulk stop (cancel all QUEUED)
 *   - bulk clear (remove all done/failed/skipped/canceled)
 *
 * Realtime: queue_snapshot and queue_item_done update the table without reload.
 */

const QUEUE_STATES = {
  queued: { label: "В очереди", tone: "warning" },
  processing: { label: "Транскрибируется", tone: "warning" },
  done: { label: "Готово", tone: "success" },
  failed: { label: "Ошибка", tone: "danger" },
  skipped: { label: "Пропущено", tone: "neutral" },
  canceled: { label: "Отменено", tone: "neutral" },
};

function formatDateTime(value) {
  if (!value) return "—";
  const [datePart, timePart = ""] = String(value).replace("T", " ").split(" ");
  const segments = datePart.split("-");
  if (segments.length !== 3) return value;
  const [year, month, day] = segments;
  return `${day}.${month}.${year} ${timePart.slice(0, 8)}`.trim();
}

function formatPhone(value) {
  const text = String(value ?? "").trim();
  return text || "—";
}

function formatDuration(seconds) {
  const total = Number(seconds) || 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(secs)}`
    : `${minutes}:${pad(secs)}`;
}
/** Case-insensitive substring match; an empty needle matches everything. */
function includesText(haystack, needle) {
  return String(haystack ?? "")
    .toLowerCase()
    .includes(String(needle ?? "").trim().toLowerCase());
}

/** Inclusive numeric range where either bound may be blank. */
function inRange(raw, { min, max }) {
  const value = Number(raw) || 0;
  if (min !== "" && value < Number(min)) return false;
  if (max !== "" && value > Number(max)) return false;
  return true;
}

/** De-duplicate select options by value, dropping empties. */
function uniqueOptions(pairs) {
  const seen = new Map();
  pairs.forEach(({ value, label }) => {
    if (value === null || value === undefined || value === "") return;
    if (!seen.has(String(value))) seen.set(String(value), label);
  });
  return Array.from(seen, ([value, label]) => ({ value, label }));
}

/**
 * Every column the queue table can render, in display order.
 *
 * Each entry owns its cell rendering *and* its filtering, so adding a column
 * means adding one object here rather than touching the header, the body and the
 * filter row separately. `render` receives the item plus a small context object
 * (currently the connection label map).
 *
 * This is presentation only: the backend keeps returning the full snapshot and
 * nothing here changes what is fetched.
 */
const COLUMNS = [
  {
    key: "datetime_start",
    label: "Дата",
    filterKind: "dateRange",
    className: "whitespace-nowrap text-slate-700",
    render: (item) => formatDateTime(item.datetime_start),
    match: (item, value) => {
      // datetime_start is upstream wall-clock text; compare the YYYY-MM-DD head
      // lexicographically instead of parsing, which would shift the timezone.
      const day = String(item.datetime_start ?? "").slice(0, 10);
      if (value.from && day < value.from) return false;
      if (value.to && day > value.to) return false;
      return true;
    },
  },
  {
    key: "call_id",
    label: "call_id",
    filterKind: "text",
    className: "font-mono text-sm text-slate-600",
    render: (item) => item.call_id,
    match: (item, value) => includesText(item.call_id, value),
  },
  {
    key: "caller_a",
    label: "caller_a",
    filterKind: "text",
    className: "text-slate-700",
    render: (item) => formatPhone(item.caller_a),
    match: (item, value) => includesText(item.caller_a, value),
  },
  {
    key: "caller_b",
    label: "caller_b",
    filterKind: "text",
    className: "text-slate-700",
    render: (item) => formatPhone(item.caller_b),
    match: (item, value) => includesText(item.caller_b, value),
  },
  {
    key: "scenario_name",
    label: "Сценарий",
    filterKind: "text",
    className: "text-slate-600",
    render: (item) => item.scenario_name || "—",
    match: (item, value) => includesText(item.scenario_name, value),
  },
  {
    key: "duration",
    label: "Длительность",
    filterKind: "range",
    align: "right",
    className: "whitespace-nowrap text-right tabular-nums text-slate-700",
    render: (item) => formatDuration(item.duration),
    match: (item, value) => inRange(item.duration, value),
  },
  {
    key: "connection_id",
    label: "Подключение",
    filterKind: "select",
    className: "text-sm text-slate-600",
    render: (item, ctx) => ctx.connectionMap[item.connection_id] || "—",
    options: (items, ctx) =>
      uniqueOptions(
        items.map((item) => ({
          value: item.connection_id,
          label: ctx.connectionMap[item.connection_id] || item.connection_id,
        })),
      ),
    match: (item, value) => String(item.connection_id) === value,
  },
  {
    key: "whisper_model",
    label: "Модель",
    filterKind: "select",
    className: "text-sm text-slate-600",
    render: (item) => item.whisper_model,
    options: (items) =>
      uniqueOptions(
        items.map((item) => ({ value: item.whisper_model, label: item.whisper_model })),
      ),
    match: (item, value) => String(item.whisper_model) === value,
  },
  {
    key: "status",
    label: "Статус",
    filterKind: "select",
    render: (item) => {
      const state = QUEUE_STATES[item.status] || { label: item.status, tone: "neutral" };
      return <Badge tone={state.tone}>{state.label}</Badge>;
    },
    options: () =>
      Object.entries(QUEUE_STATES).map(([value, state]) => ({
        value,
        label: state.label,
      })),
    match: (item, value) => item.status === value,
  },
  {
    key: "attempts",
    label: "Попыток",
    filterKind: "number",
    className: "text-xs text-slate-500",
    render: (item) => (item.attempts > 1 ? `×${item.attempts}` : "—"),
    match: (item, value) => Number(item.attempts || 0) >= Number(value),
  },
];

const COLUMN_KEYS = COLUMNS.map((column) => column.key);

/** Columns shown to a user who has never touched the picker. */
const DEFAULT_COLUMN_KEYS = [
  "datetime_start",
  "caller_a",
  "caller_b",
  "duration",
  "status",
];

function defaultQueueColumns() {
  return [...DEFAULT_COLUMN_KEYS];
}

/** The "no restriction" value for one column's filter kind. */
function defaultFilterValue(column) {
  if (column.filterKind === "dateRange") return { from: "", to: "" };
  if (column.filterKind === "range") return { min: "", max: "" };
  return "";
}

function defaultQueueFilters() {
  const filters = {};
  COLUMNS.forEach((column) => {
    filters[column.key] = defaultFilterValue(column);
  });
  return filters;
}

/** A filter counts as active only when it would actually exclude something. */
function isFilterActive(kind, value) {
  if (value === null || value === undefined) return false;
  if (kind === "dateRange") return Boolean(value.from || value.to);
  if (kind === "range") return value.min !== "" || value.max !== "";
  return String(value) !== "";
}

/** Keep only known column keys; an empty or broken list falls back to defaults. */
function sanitizeColumns(stored) {
  if (!Array.isArray(stored)) return defaultQueueColumns();
  const valid = stored.filter((key) => COLUMN_KEYS.includes(key));
  return valid.length > 0 ? valid : defaultQueueColumns();
}

/** Merge stored filters over a fresh default so missing keys cannot crash a control. */
function sanitizeFilters(stored) {
  const base = defaultQueueFilters();
  if (!stored || typeof stored !== "object") return base;

  COLUMNS.forEach((column) => {
    const value = stored[column.key];
    if (value === undefined || value === null) return;

    if (column.filterKind === "dateRange" && typeof value === "object") {
      base[column.key] = { from: String(value.from ?? ""), to: String(value.to ?? "") };
    } else if (column.filterKind === "range" && typeof value === "object") {
      base[column.key] = { min: String(value.min ?? ""), max: String(value.max ?? "") };
    } else if (typeof value === "string" || typeof value === "number") {
      base[column.key] = String(value);
    }
  });

  return base;
}

/**
 * One filter control, chosen by the column's declared filter kind.
 *
 * Kept deliberately small: these live in a row directly under the header, so a
 * tall or chatty control would push the data out of view.
 */
function ColumnFilter({ column, value, onChange, items, ctx }) {
  const base =
    "w-full rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 focus:outline-none focus-visible:ring-1 focus-visible:ring-slate-500";

  if (column.filterKind === "select") {
    const options = column.options ? column.options(items, ctx) : [];
    return (
      <select
        className={base}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={`Фильтр: ${column.label}`}
      >
        <option value="">Все</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    );
  }

  if (column.filterKind === "dateRange") {
    return (
      <div className="flex gap-1">
        <input
          type="date"
          className={base}
          value={value.from}
          onChange={(e) => onChange({ ...value, from: e.target.value })}
          aria-label={`${column.label}: с`}
        />
        <input
          type="date"
          className={base}
          value={value.to}
          onChange={(e) => onChange({ ...value, to: e.target.value })}
          aria-label={`${column.label}: по`}
        />
      </div>
    );
  }

  if (column.filterKind === "range") {
    return (
      <div className="flex gap-1">
        <input
          type="number"
          min="0"
          placeholder="от"
          className={base}
          value={value.min}
          onChange={(e) => onChange({ ...value, min: e.target.value })}
          aria-label={`${column.label}: минимум, секунд`}
        />
        <input
          type="number"
          min="0"
          placeholder="до"
          className={base}
          value={value.max}
          onChange={(e) => onChange({ ...value, max: e.target.value })}
          aria-label={`${column.label}: максимум, секунд`}
        />
      </div>
    );
  }

  if (column.filterKind === "number") {
    return (
      <input
        type="number"
        min="0"
        placeholder="≥"
        className={base}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={`Фильтр: ${column.label}`}
      />
    );
  }

  return (
    <input
      type="text"
      placeholder="поиск"
      className={base}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={`Фильтр: ${column.label}`}
    />
  );
}


function QueueItemRow({
  item,
  index,
  columns,
  ctx,
  onCancel,
  onRetry,
  onDelete,
  busy,
  isExpanded,
  onToggle,
}) {
  // The status cell itself is rendered by the COLUMNS registry; this component
  // only needs the status to decide which actions are available.
  const canCancel = item.status === "queued" && !busy;
  const canRetry =
    (item.status === "failed" ||
      item.status === "skipped" ||
      item.status === "canceled") &&
    !busy;
  const canDelete = item.status !== "processing" && !busy;
  const hasError = item.status === "failed" && item.error_message;
  const hasTranscript = item.status === "done" && item.transcript_text;
  const canExpand = hasError || hasTranscript;

  // Expansion row must span all visible data columns plus the two fixed columns.
  const totalColSpan = columns.length + 2;

  return (
    <>
      <tr className="border-b border-slate-100 align-top hover:bg-slate-50">
        <td className="px-3 py-3 text-slate-400">{index}</td>
        {columns.map((column) => (
          <td key={column.key} className={`px-3 py-3 ${column.className || ""}`}>
            {column.render(item, ctx)}
          </td>
        ))}
        <td className="px-3 py-3 text-right">
          <div className="flex flex-wrap items-center justify-end gap-2">
            {canCancel && (
              <Button variant="danger" onClick={() => onCancel(item.id)}>
                Отменить
              </Button>
            )}
            {canRetry && (
              <Button variant="secondary" onClick={() => onRetry(item.id)}>
                Повторить
              </Button>
            )}
            {canExpand && (
              <Button variant="secondary" onClick={onToggle}>
                {isExpanded
                  ? hasTranscript
                    ? "Скрыть транскрипцию"
                    : "Скрыть ошибку"
                  : hasTranscript
                    ? "Показать транскрипцию"
                    : "Показать ошибку"}
              </Button>
            )}
            {canDelete && (
              <Button variant="danger" onClick={() => onDelete(item.id)}>
                Удалить
              </Button>
            )}
          </div>
        </td>
      </tr>

      {isExpanded && canExpand && (
        <tr className="border-b border-slate-100 bg-slate-50">
          <td />
          <td colSpan={totalColSpan} className="px-3 pb-4">
            {hasError && (
              <>
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-red-600">
                  Ошибка
                </p>
                <p className="text-sm leading-relaxed text-red-800">{item.error_message}</p>
              </>
            )}
            {hasTranscript && (
              <>
                <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
                  Транскрипция
                </p>
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800">
                  {item.transcript_text}
                </p>
              </>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

export default function QueuePage() {
  const { subscribe } = useEvents();

  const [snapshot, setSnapshot] = useState(null);
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [expandedIds, setExpandedIds] = useState(() => new Set());

  const toggleExpanded = useCallback((id) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  // Table presentation preferences. Non-secret, so localStorage is fine, and
  // they are not connection-scoped: the queue itself spans every connection.
  const [visibleColumns, setVisibleColumns] = usePersistedState(
    uiKey("queueColumns"),
    defaultQueueColumns,
    sanitizeColumns,
  );
  const [columnFilters, setColumnFilters] = usePersistedState(
    uiKey("queueFilters"),
    defaultQueueFilters,
    sanitizeFilters,
  );

  const setFilterValue = useCallback(
    (key, value) => {
      setColumnFilters((prev) => ({ ...prev, [key]: value }));
    },
    [setColumnFilters],
  );

  const toggleColumn = useCallback(
    (key) => {
      setVisibleColumns((prev) => {
        if (prev.includes(key)) {
          // Prevent hiding the last visible column.
          if (prev.length === 1) return prev;
          // Reset this column's filter when hiding it; no invisible restrictions.
          const column = COLUMNS.find((col) => col.key === key);
          if (column) {
            setColumnFilters((filters) => ({
              ...filters,
              [key]: defaultFilterValue(column),
            }));
          }
          return prev.filter((item) => item !== key);
        }
        // Re-derive order from COLUMNS so the header never depends on the
        // order in which the user ticked the boxes.
        return COLUMN_KEYS.filter((item) => prev.includes(item) || item === key);
      });
    },
    [setVisibleColumns, setColumnFilters],
  );

  const resetFilters = useCallback(() => {
    setColumnFilters(defaultQueueFilters());
  }, [setColumnFilters]);

  // Render in registry order, ignoring any stale key.
  const activeColumns = useMemo(
    () => COLUMNS.filter((column) => visibleColumns.includes(column.key)),
    [visibleColumns],
  );

  // Only filters belonging to a visible column apply; hiding a column must not
  // leave an invisible filter silently removing rows.
  const activeFilters = useMemo(
    () =>
      activeColumns
        .map((column) => ({ column, value: columnFilters[column.key] }))
        .filter(({ column, value }) => isFilterActive(column.filterKind, value)),
    [activeColumns, columnFilters],
  );

  const connectionMap = useMemo(
    () =>
      connections.reduce((map, conn) => {
        map[conn.id] = conn.label;
        return map;
      }, {}),
    [connections],
  );

  // Must compute these derived values *before* any early return, or React will
  // see a different hook count on the loading path and the loaded path.
  const items = snapshot?.items || [];
  const counters = snapshot?.counters || {};
  const columnContext = { connectionMap };

  const filteredItems = useMemo(() => {
    if (activeFilters.length === 0) return items;
    return items.filter((item) =>
      activeFilters.every(({ column, value }) => column.match(item, value)),
    );
  }, [items, activeFilters]);

  const isEmpty = filteredItems.length === 0 && activeFilters.length === 0;
  const hasNoResults = filteredItems.length === 0 && activeFilters.length > 0;

  const loadQueue = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await queueApi.get();
      setSnapshot(data);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadConnections = useCallback(async () => {
    try {
      const data = await connectionsApi.list();
      setConnections(data);
    } catch (err) {
      setError(err);
    }
  }, []);

  useEffect(() => {
    loadQueue();
    loadConnections();
  }, [loadQueue, loadConnections]);

  const cancelItem = useCallback(
    async (itemId) => {
      setBusy(true);
      setError(null);
      try {
        await queueApi.cancelItem(itemId);
        // Snapshot arrives via WS.
      } catch (err) {
        setError(err);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const retryItem = useCallback(
    async (itemId) => {
      setBusy(true);
      setError(null);
      try {
        await queueApi.retryItem(itemId);
      } catch (err) {
        setError(err);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const deleteItem = useCallback(
    async (itemId) => {
      if (!window.confirm("Удалить эту задачу?")) return;
      setBusy(true);
      setError(null);
      try {
        await queueApi.deleteItem(itemId);
        await loadQueue();
      } catch (err) {
        setError(err);
      } finally {
        setBusy(false);
      }
    },
    [loadQueue],
  );

  const retryAll = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await queueApi.retry();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, []);

  const stopAll = useCallback(async () => {
    if (!window.confirm("Отменить все ожидающие задачи?")) return;
    setBusy(true);
    setError(null);
    try {
      await queueApi.stop();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, []);

  const clearCompleted = useCallback(async () => {
    if (!window.confirm("Удалить все завершённые задачи?")) return;
    setBusy(true);
    setError(null);
    try {
      await queueApi.clear(["done", "failed", "skipped", "canceled"]);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, []);

  // Realtime updates.
  useEffect(() => {
    return subscribe((payload) => {
      if (payload.type === "queue_snapshot" && payload.snapshot) {
        setSnapshot(payload.snapshot);
      }
    });
  }, [subscribe]);

  if (loading && !snapshot) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Очередь</h1>
          <p className="mt-1 text-sm text-slate-500">Загрузка...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Очередь</h1>
        <p className="mt-1 text-sm text-slate-500">
          Задачи транскрибации и их статусы.
        </p>
      </div>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {/* Counters */}
      <div className="grid gap-4 sm:grid-cols-3 md:grid-cols-6">
        <Card>
          <p className="text-sm text-slate-500">Всего</p>
          <p className="mt-1 text-2xl font-semibold text-slate-900">
            {counters.total || 0}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-slate-500">В очереди</p>
          <p className="mt-1 text-2xl font-semibold text-amber-600">
            {counters.queued || 0}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-slate-500">Выполняется</p>
          <p className="mt-1 text-2xl font-semibold text-amber-700">
            {counters.processing || 0}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-slate-500">Готово</p>
          <p className="mt-1 text-2xl font-semibold text-emerald-600">
            {counters.done || 0}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-slate-500">Ошибки</p>
          <p className="mt-1 text-2xl font-semibold text-red-600">
            {counters.failed || 0}
          </p>
        </Card>
        <Card>
          <p className="text-sm text-slate-500">Прочие</p>
          <p className="mt-1 text-2xl font-semibold text-slate-500">
            {(counters.skipped || 0) + (counters.canceled || 0)}
          </p>
        </Card>
      </div>

      {/* Actions */}
      {!isEmpty && (
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={retryAll} disabled={busy || counters.failed === 0}>
              Повторить ошибки
            </Button>
            <Button variant="danger" onClick={stopAll} disabled={busy || counters.queued === 0}>
              Отменить ожидающие
            </Button>
            <Button variant="danger" onClick={clearCompleted} disabled={busy}>
              Очистить завершённые
            </Button>
          </div>
        </Card>
      )}

      {/* Table */}
      {isEmpty ? (
        <Card>
          <EmptyState
            title="Очередь пуста"
            description="Добавьте звонки для транскрибации в разделе «Звонки»."
          />
          <div className="mt-4 flex justify-center">
            <Link to="/calls">
              <Button>Перейти к звонкам</Button>
            </Link>
          </div>
        </Card>
      ) : (
        <Card>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <Popover label={`Колонки (${visibleColumns.length}/${COLUMN_KEYS.length})`}>
              <div className="space-y-2">
                {COLUMNS.map((column) => (
                  <Checkbox
                    key={column.key}
                    label={column.label}
                    checked={visibleColumns.includes(column.key)}
                    onChange={() => toggleColumn(column.key)}
                  />
                ))}
              </div>
            </Popover>

            {activeFilters.length > 0 && (
              <Button variant="secondary" onClick={resetFilters}>
                Сбросить фильтры ({activeFilters.length})
              </Button>
            )}
          </div>

          {hasNoResults ? (
            <EmptyState
              title="Нет результатов"
              description="Попробуйте изменить или сбросить фильтры."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-3 py-3">#</th>
                    {activeColumns.map((column) => (
                      <th
                        key={column.key}
                        className={`px-3 py-3 ${column.align === "right" ? "text-right" : ""}`}
                      >
                        {column.label}
                      </th>
                    ))}
                    <th className="px-3 py-3 text-right">Действия</th>
                  </tr>
                  <tr>
                    <th className="px-3 py-2" />
                    {activeColumns.map((column) => (
                      <th key={column.key} className="px-3 py-2">
                        <ColumnFilter
                          column={column}
                          value={columnFilters[column.key]}
                          onChange={(value) => setFilterValue(column.key, value)}
                          items={items}
                          ctx={columnContext}
                        />
                      </th>
                    ))}
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {filteredItems.map((item, index) => (
                    <QueueItemRow
                      key={item.id}
                      item={item}
                      index={index + 1}
                      columns={activeColumns}
                      ctx={columnContext}
                      onCancel={cancelItem}
                      onRetry={retryItem}
                      onDelete={deleteItem}
                      busy={busy}
                      isExpanded={expandedIds.has(item.id)}
                      onToggle={() => toggleExpanded(item.id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
