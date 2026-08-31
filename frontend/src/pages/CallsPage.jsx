import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "../api/client";
import { callsApi, connectionsApi, queueApi } from "../api/endpoints";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  ErrorBanner,
  Field,
  Select,
  TextInput,
} from "../components/ui";
import { useEvents } from "../realtime/EventsContext";
import { Link } from "../router";
import { useSelectedConnection } from "../state/SelectedConnectionContext";

/**
 * Call history for the selected Voximplant Kit connection.
 *
 * Data flow, deliberately server-mediated:
 *   POST /api/calls/search      -> normalized calls (no upstream record URLs)
 *   POST /api/calls/audio-url   -> short-lived HMAC-signed playback URL
 *   POST /api/queue/add         -> background transcription, progress over WS
 *
 * The browser never holds a Voximplant token, never talks to Voximplant, and
 * never receives an upstream recording URL. The only identifier it sends is a
 * connection id it owns plus opaque call ids.
 *
 * Filters mirror what the Kit history API actually supports (date range,
 * scenario) plus the two post-filters the backend applies (minimum duration,
 * "has recording"). Nothing here invents a filter the upstream cannot honour.
 */

// The upstream page size is 50; these are per-request accumulation targets.
const PAGE_SIZES = [25, 50, 100, 200];

const QUEUE_STATES = {
  queued: { label: "в очереди", tone: "warning" },
  processing: { label: "транскрибируется", tone: "warning" },
  done: { label: "готово", tone: "success" },
  failed: { label: "ошибка", tone: "danger" },
  skipped: { label: "пропущено", tone: "neutral" },
  canceled: { label: "отменено", tone: "neutral" },
};

/** `YYYY-MM-DDTHH:MM` in local time, which is what datetime-local expects. */
function toLocalInput(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function defaultFilters() {
  const now = new Date();
  const dayAgo = new Date(now.getTime() - 24 * 60 * 60 * 1000);
  return {
    from_date: toLocalInput(dayAgo),
    to_date: toLocalInput(now),
    scenario_id: "",
    min_duration: 0,
    has_recording: true,
    records_limit: 50,
  };
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

/**
 * Render an upstream timestamp without converting it.
 *
 * The Kit API returns wall-clock time for the account's timezone. Feeding it to
 * `new Date()` would re-interpret it in the browser's zone and silently shift
 * every row, so the string is reformatted textually instead.
 */
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
/**
 * Inline recording player.
 *
 * The signed URL is minted lazily on first play, not while rendering the table:
 * a grant has a short TTL (900s by default), so issuing one for every visible
 * row would hand out links that expire before use. Once loaded, the native
 * <audio> element provides play/pause, seeking and duration for free.
 */
function AudioPlayer({ connectionId, callId, onError }) {
  const [src, setSrc] = useState(null);
  const [loading, setLoading] = useState(false);
  const audioRef = useRef(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await callsApi.audioUrl(connectionId, callId);
      setSrc(data.audio_url);
    } catch (error) {
      onError(error);
    } finally {
      setLoading(false);
    }
  };

  // Autoplay once the element has a source, so a single click starts playback.
  useEffect(() => {
    if (src && audioRef.current) {
      audioRef.current.play().catch(() => {
        // Autoplay policy refused it; the visible controls still work.
      });
    }
  }, [src]);

  if (!src) {
    return (
      <Button variant="secondary" onClick={load} disabled={loading}>
        {loading ? "Загрузка..." : "Прослушать"}
      </Button>
    );
  }

  return (
    <audio
      ref={audioRef}
      controls
      preload="metadata"
      src={src}
      className="h-9 w-64 max-w-full"
      aria-label={`Запись звонка ${callId}`}
      onError={() =>
        onError(
          new ApiError(
            0,
            "AUDIO_UNAVAILABLE",
            "Не удалось загрузить запись. Обновите поиск и попробуйте снова",
          ),
        )
      }
    >
      Ваш браузер не поддерживает воспроизведение аудио.
    </audio>
  );
}

/** One call, plus its expandable recording player and transcript. */
function CallRow({
  call,
  index,
  connectionId,
  queueStatus,
  isExpanded,
  onToggle,
  onTranscribe,
  onError,
  busy,
}) {
  const state = queueStatus ? QUEUE_STATES[queueStatus] : null;
  const hasTranscript = Boolean(call.transcript);
  const isRunning = queueStatus === "queued" || queueStatus === "processing";

  return (
    <>
      <tr className="border-b border-slate-100 align-top hover:bg-slate-50">
        <td className="px-3 py-3 text-slate-400">{index}</td>
        <td className="whitespace-nowrap px-3 py-3 text-slate-700">
          {formatDateTime(call.datetime_start)}
          {call.timezone && (
            <span className="ml-1 text-xs text-slate-400">{call.timezone}</span>
          )}
        </td>
        <td className="px-3 py-3 text-slate-700">{formatPhone(call.phone_a)}</td>
        <td className="px-3 py-3 text-slate-700">{formatPhone(call.phone_b)}</td>
        <td className="px-3 py-3 text-slate-600">{call.scenario_name || "—"}</td>
        <td className="whitespace-nowrap px-3 py-3 text-right tabular-nums text-slate-700">
          {formatDuration(call.duration)}
        </td>
        <td className="px-3 py-3">
          {call.has_recording ? (
            <Badge tone="success">есть</Badge>
          ) : (
            <Badge tone="neutral">нет</Badge>
          )}
        </td>
        <td className="px-3 py-3">
          {hasTranscript ? (
            <Badge tone="success">готово</Badge>
          ) : state ? (
            <Badge tone={state.tone}>{state.label}</Badge>
          ) : (
            <Badge tone="neutral">нет</Badge>
          )}
        </td>
        <td className="px-3 py-3">
          <div className="flex flex-wrap items-center justify-end gap-2">
            {call.has_recording && (
              <AudioPlayer
                connectionId={connectionId}
                callId={call.id}
                onError={onError}
              />
            )}

            {call.has_recording && !hasTranscript && (
              <Button onClick={() => onTranscribe(call)} disabled={busy || isRunning}>
                {isRunning ? "В работе..." : "Транскрибировать"}
              </Button>
            )}

            {hasTranscript && (
              <Button variant="secondary" onClick={onToggle} aria-expanded={isExpanded}>
                {isExpanded ? "Скрыть транскрипцию" : "Показать транскрипцию"}
              </Button>
            )}
          </div>
        </td>
      </tr>

      {isExpanded && hasTranscript && (
        <tr className="border-b border-slate-100 bg-slate-50">
          <td />
          <td colSpan={8} className="px-3 pb-4">
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
              Транскрипция
            </p>
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800">
              {call.transcript}
            </p>
          </td>
        </tr>
      )}
    </>
  );
}

export default function CallsPage() {
  const { selectedId: connectionId } = useSelectedConnection();
  const { subscribe } = useEvents();

  const [connections, setConnections] = useState([]);
  const [loadingConnections, setLoadingConnections] = useState(false);

  const [filters, setFilters] = useState(defaultFilters);
  const [scenarios, setScenarios] = useState([]);

  const [calls, setCalls] = useState([]);
  const [searchStatus, setSearchStatus] = useState("idle"); // idle | searching | done
  const [cursor, setCursor] = useState(null);
  const [canLoadMore, setCanLoadMore] = useState(false);
  const [totalLoaded, setTotalLoaded] = useState(0);

  // Queue status keyed by call_id, updated live via the socket.
  const [queueMap, setQueueMap] = useState({});
  // Actively adding one call to the queue prevents concurrent adds.
  const [adding, setAdding] = useState(false);
  // The expanded transcript row, if any.
  const [expandedId, setExpandedId] = useState(null);
  const [error, setError] = useState(null);

  const selectedConnection = useMemo(
    () => connections.find((item) => item.id === connectionId),
    [connections, connectionId],
  );

  // Load owned connections once.
  const loadConnections = useCallback(async () => {
    setLoadingConnections(true);
    try {
      const data = await connectionsApi.list();
      setConnections(data);
    } catch (err) {
      setError(err);
    } finally {
      setLoadingConnections(false);
    }
  }, []);

  useEffect(() => {
    loadConnections();
  }, [loadConnections]);

  // Load scenarios for the filter dropdown.
  const loadScenarios = useCallback(async () => {
    if (!connectionId) return;
    try {
      const data = await callsApi.scenarios(connectionId);
      setScenarios(data);
    } catch (err) {
      setError(err);
    }
  }, [connectionId]);

  useEffect(() => {
    if (connectionId) loadScenarios();
  }, [connectionId, loadScenarios]);

  // Search calls, replacing the current page.
  const search = useCallback(async () => {
    if (!connectionId) return;
    setSearchStatus("searching");
    setError(null);
    setCalls([]);
    setCursor(null);
    setCanLoadMore(false);
    setTotalLoaded(0);

    try {
      const response = await callsApi.search(
        {
          connection_id: connectionId,
          from_date: filters.from_date,
          to_date: filters.to_date,
          scenario_id: filters.scenario_id ? Number(filters.scenario_id) : null,
          min_duration: filters.min_duration,
          has_recording: filters.has_recording,
          records_limit: filters.records_limit,
        },
        null,
      );

      setCalls(response.items || []);
      setTotalLoaded(response.total_loaded || 0);
      setCursor(response.cursor || null);
      setCanLoadMore(response.can_load_more ?? false);
      setSearchStatus("done");
    } catch (err) {
      setError(err);
      setSearchStatus("idle");
    }
  }, [connectionId, filters]);

  // Load more calls, appending to the current page.
  const loadMore = useCallback(async () => {
    if (!connectionId || !cursor) return;
    setSearchStatus("searching");
    setError(null);

    try {
      const response = await callsApi.search(
        {
          connection_id: connectionId,
          from_date: filters.from_date,
          to_date: filters.to_date,
          scenario_id: filters.scenario_id ? Number(filters.scenario_id) : null,
          min_duration: filters.min_duration,
          has_recording: filters.has_recording,
          records_limit: filters.records_limit,
        },
        cursor,
      );

      setCalls((prev) => [...prev, ...(response.items || [])]);
      setTotalLoaded(response.total_loaded || 0);
      setCursor(response.cursor || null);
      setCanLoadMore(response.can_load_more ?? false);
      setSearchStatus("done");
    } catch (err) {
      setError(err);
      setSearchStatus("done");
    }
  }, [connectionId, cursor, filters]);

  // Add one call to the background transcription queue.
  const transcribe = useCallback(
    async (call) => {
      if (!connectionId) return;
      setAdding(true);
      setError(null);

      try {
        await queueApi.add(connectionId, [call.id]);
        // The queue snapshot will arrive over the socket and update queueMap.
      } catch (err) {
        setError(err);
      } finally {
        setAdding(false);
      }
    },
    [connectionId],
  );

  // Subscribe to live queue events and merge them into local state.
  useEffect(() => {
    return subscribe((payload) => {
      if (payload.type === "queue_snapshot" && payload.snapshot) {
        const map = {};
        (payload.snapshot.items || []).forEach((item) => {
          map[item.call_id] = item.status;
        });
        setQueueMap(map);
      } else if (payload.type === "queue_item_done" && payload.item) {
        const { call_id, status, transcript_text } = payload.item;
        // Live-refresh the transcript in the table so reload is not needed.
        setCalls((prev) =>
          prev.map((call) =>
            call.id === call_id && !call.transcript && transcript_text
              ? { ...call, transcript: transcript_text }
              : call,
          ),
        );
        setQueueMap((prev) => ({ ...prev, [call_id]: status }));
      }
    });
  }, [subscribe]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Звонки</h1>
        <p className="mt-1 text-sm text-slate-500">
          Поиск и транскрибация звонков Voximplant Kit.
        </p>
      </div>

      {!connectionId && (
        <Card>
          <EmptyState
            title="Сначала выберите подключение"
            description="Перейдите в раздел «Подключения», чтобы создать или выбрать подключение Voximplant Kit."
          />
          <div className="mt-4 flex justify-center">
            <Link to="/connections">
              <Button>Перейти к подключениям</Button>
            </Link>
          </div>
        </Card>
      )}

      {connectionId && !selectedConnection && !loadingConnections && (
        <Card>
          <EmptyState
            title="Выбранное подключение недоступно"
            description="Подключение могло быть удалено. Выберите другое или создайте новое."
          />
          <div className="mt-4 flex justify-center">
            <Link to="/connections">
              <Button>Перейти к подключениям</Button>
            </Link>
          </div>
        </Card>
      )}

      {selectedConnection && (
        <>
          <ErrorBanner error={error} onDismiss={() => setError(null)} />

          <Card title="Фильтры">
            <div className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <Field label="Дата и время с">
                  <TextInput
                    type="datetime-local"
                    value={filters.from_date}
                    onChange={(e) =>
                      setFilters({ ...filters, from_date: e.target.value })
                    }
                  />
                </Field>

                <Field label="Дата и время по">
                  <TextInput
                    type="datetime-local"
                    value={filters.to_date}
                    onChange={(e) => setFilters({ ...filters, to_date: e.target.value })}
                  />
                </Field>
              </div>

              <Field label="Сценарий">
                <Select
                  value={filters.scenario_id}
                  onChange={(e) =>
                    setFilters({ ...filters, scenario_id: e.target.value })
                  }
                >
                  <option value="">Все сценарии</option>
                  {scenarios.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.title}
                    </option>
                  ))}
                </Select>
              </Field>

              <div className="grid gap-4 md:grid-cols-[1fr_auto_auto]">
                <Field label="Мин. длительность (секунд)">
                  <TextInput
                    type="number"
                    min="0"
                    value={filters.min_duration}
                    onChange={(e) =>
                      setFilters({ ...filters, min_duration: Number(e.target.value) })
                    }
                  />
                </Field>

                <Field label="Записей на странице">
                  <Select
                    value={filters.records_limit}
                    onChange={(e) =>
                      setFilters({ ...filters, records_limit: Number(e.target.value) })
                    }
                  >
                    {PAGE_SIZES.map((size) => (
                      <option key={size} value={size}>
                        {size}
                      </option>
                    ))}
                  </Select>
                </Field>

                <div className="flex items-end pb-1">
                  <Checkbox
                    label="Только с записью"
                    checked={filters.has_recording}
                    onChange={(e) =>
                      setFilters({ ...filters, has_recording: e.target.checked })
                    }
                  />
                </div>
              </div>

              <Button
                onClick={search}
                disabled={searchStatus === "searching" || !selectedConnection.is_active}
              >
                {searchStatus === "searching" ? "Поиск..." : "Найти звонки"}
              </Button>
            </div>
          </Card>

          {searchStatus === "done" && calls.length === 0 && (
            <Card>
              <EmptyState
                title="Звонки не найдены"
                description="Попробуйте изменить фильтры или расширить диапазон дат."
              />
            </Card>
          )}

          {calls.length > 0 && (
            <Card>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-3 py-3">#</th>
                      <th className="px-3 py-3">Дата</th>
                      <th className="px-3 py-3">caller_a</th>
                      <th className="px-3 py-3">caller_b</th>
                      <th className="px-3 py-3">Сценарий</th>
                      <th className="px-3 py-3 text-right">Длительность</th>
                      <th className="px-3 py-3">Запись</th>
                      <th className="px-3 py-3">Транскрипция</th>
                      <th className="px-3 py-3 text-right">Действия</th>
                    </tr>
                  </thead>
                  <tbody>
                    {calls.map((call, index) => (
                      <CallRow
                        key={call.id}
                        call={call}
                        index={index + 1}
                        connectionId={connectionId}
                        queueStatus={queueMap[call.id]}
                        isExpanded={expandedId === call.id}
                        onToggle={() =>
                          setExpandedId(expandedId === call.id ? null : call.id)
                        }
                        onTranscribe={transcribe}
                        onError={setError}
                        busy={adding}
                      />
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 pt-4">
                <p className="text-sm text-slate-500">
                  Загружено: <span className="font-medium">{totalLoaded}</span>
                </p>

                {canLoadMore && (
                  <Button onClick={loadMore} disabled={searchStatus === "searching"}>
                    {searchStatus === "searching" ? "Загрузка..." : "Загрузить ещё"}
                  </Button>
                )}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}


