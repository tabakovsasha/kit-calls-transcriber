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
  IconButton,
  PlayIcon,
  Select,
  TextInput,
} from "../components/ui";
import { useEvents } from "../realtime/EventsContext";
import { Link } from "../router";
import { useCallsState } from "../state/CallsStateContext";
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
 * Filters and the loaded result set are not owned by this component: they live in
 * CallsStateContext, above the router, and are persisted per connection. Leaving
 * for /queue and coming back — or reloading the tab — therefore keeps both.
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

/** Number of <th> cells in the table; expansion rows span all of them. */
const COLUMN_COUNT = 10;

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
 * Full-width recording player, rendered as its own table row.
 *
 * The signed URL is minted when this component mounts, and it only mounts after
 * the user clicks the play icon. That preserves the lazy-grant rule: a grant has
 * a short TTL (900s by default), so issuing one per visible row would hand out
 * links that expire before use. The URL is never persisted — after a reload the
 * user clicks again and gets a fresh grant.
 */
function AudioPlayerRow({ connectionId, callId, onError }) {
  const [src, setSrc] = useState(null);
  const [loading, setLoading] = useState(true);
  const audioRef = useRef(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const data = await callsApi.audioUrl(connectionId, callId);
        if (!cancelled) setSrc(data.audio_url);
      } catch (error) {
        if (!cancelled) onError(error);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [connectionId, callId, onError]);

  // Autoplay once a source exists, so the click that opened the row also starts
  // playback. A refusal by the autoplay policy is harmless: controls still work.
  useEffect(() => {
    if (src && audioRef.current) {
      audioRef.current.play().catch(() => {});
    }
  }, [src]);

  return (
    <tr className="border-b border-slate-100 bg-slate-50">
      <td colSpan={COLUMN_COUNT} className="px-3 py-3">
        <div className="flex items-center gap-3">
          <span className="shrink-0 text-xs font-medium uppercase tracking-wide text-slate-500">
            Запись
          </span>

          {loading && <span className="text-sm text-slate-500">Загрузка...</span>}

          {src && (
            /* w-full is the point of this row: the player spans the whole table
               instead of being squeezed into an action cell. */
            <audio
              ref={audioRef}
              controls
              preload="metadata"
              src={src}
              className="h-10 w-full"
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
          )}

          {!loading && !src && (
            <span className="text-sm text-red-600">Запись недоступна</span>
          )}
        </div>
      </td>
    </tr>
  );
}

/**
 * One call row, plus up to two independent expansion rows below it.
 *
 * Audio and transcript expand separately; opening one never collapses the other.
 * Both support multiple open rows at once across the table, so the user can read
 * several transcripts or play several recordings without dismissing the others.
 */
function CallRow({
  call,
  index,
  connectionId,
  queueStatus,
  audioOpen,
  transcriptOpen,
  onToggleAudio,
  onToggleTranscript,
  onTranscribe,
  onError,
  busy,
  selected,
  onToggleSelection,
}) {
  const state = queueStatus ? QUEUE_STATES[queueStatus] : null;
  const hasTranscript = Boolean(call.transcript);
  const isRunning = queueStatus === "queued" || queueStatus === "processing";

  return (
    <>
      <tr className="border-b border-slate-100 align-top hover:bg-slate-50">
        <td className="px-3 py-3 text-slate-400">{index}</td>
        <td className="px-3 py-3">
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelection(call.id)}
            className="h-4 w-4 rounded border-slate-300"
            aria-label={`Выбрать звонок ${call.id}`}
          />
        </td>
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
              <IconButton
                label="Прослушать запись"
                active={audioOpen}
                onClick={onToggleAudio}
              >
                <PlayIcon />
              </IconButton>
            )}

            {call.has_recording && !hasTranscript && (
              <Button onClick={() => onTranscribe(call)} disabled={busy || isRunning}>
                {isRunning ? "В работе..." : "Транскрибировать"}
              </Button>
            )}

            {hasTranscript && (
              <Button variant="secondary" onClick={onToggleTranscript}>
                {transcriptOpen ? "Скрыть транскрипцию" : "Показать транскрипцию"}
              </Button>
            )}
          </div>
        </td>
      </tr>

      {audioOpen && call.has_recording && (
        <AudioPlayerRow
          connectionId={connectionId}
          callId={call.id}
          onError={onError}
        />
      )}

      {transcriptOpen && hasTranscript && (
        <tr className="border-b border-slate-100 bg-slate-50">
          <td colSpan={COLUMN_COUNT} className="px-3 pb-4">
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

  // Filters and the loaded result set live above the router, so leaving /calls
  // and coming back does not reset them. Both are also persisted per connection.
  const {
    filters,
    results,
    searchedAt,
    setFilters,
    setResults,
    appendResults,
    updateCall,
  } = useCallsState();

  const [connections, setConnections] = useState([]);
  const [loadingConnections, setLoadingConnections] = useState(false);
  const [scenarios, setScenarios] = useState([]);

  // Result set, derived from the persisted store rather than local state.
  const calls = results?.calls ?? [];
  const cursor = results?.cursor ?? null;
  const canLoadMore = results?.canLoadMore ?? false;
  const totalLoaded = results?.totalLoaded ?? 0;

  // "done" as soon as a restored result set exists, so the empty-state card does
  // not flash over cached rows after a reload.
  const [searchStatus, setSearchStatus] = useState(() =>
    calls.length > 0 ? "done" : "idle",
  );

  // Queue status keyed by call_id, updated live via the socket.
  const [queueMap, setQueueMap] = useState({});
  // Actively adding to the queue; blocks concurrent adds.
  const [adding, setAdding] = useState(false);
  // Audio and transcript expansion are tracked separately and both allow many
  // open rows at once, so opening one never collapses another.
  const [expandedAudioIds, setExpandedAudioIds] = useState(() => new Set());
  const [expandedTranscriptIds, setExpandedTranscriptIds] = useState(() => new Set());
  // Multi-selection for batch transcription.
  const [selectedCallIds, setSelectedCallIds] = useState(() => new Set());
  // Human-readable outcome of the last batch add ("Добавлено: 10. Пропущено: 2").
  const [batchResult, setBatchResult] = useState(null);
  const [error, setError] = useState(null);

  const selectedConnection = useMemo(
    () => connections.find((item) => item.id === connectionId),
    [connections, connectionId],
  );

  // Switching connection must not carry per-row UI state across scopes: the row
  // ids belong to the previous connection's result set.
  useEffect(() => {
    setExpandedAudioIds(new Set());
    setExpandedTranscriptIds(new Set());
    setSelectedCallIds(new Set());
    setBatchResult(null);
    setSearchStatus(calls.length > 0 ? "done" : "idle");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId]);

  const toggleId = useCallback((setter, id) => {
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

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

  // Search calls, replacing the current page and persisting the result.
  const search = useCallback(async () => {
    if (!connectionId) return;
    setSearchStatus("searching");
    setError(null);
    setSelectedCallIds(new Set());
    setBatchResult(null);

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

      setResults(
        response.items || [],
        response.cursor || null,
        response.can_load_more ?? false,
        response.total_loaded || 0,
      );
      setSearchStatus("done");
    } catch (err) {
      setError(err);
      setSearchStatus("idle");
    }
  }, [connectionId, filters, setResults]);

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

      appendResults(
        response.items || [],
        response.cursor || null,
        response.can_load_more ?? false,
        response.total_loaded || 0,
      );
      setSearchStatus("done");
    } catch (err) {
      setError(err);
      setSearchStatus("done");
    }
  }, [connectionId, cursor, filters, appendResults]);


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

  // Add selected calls to the queue in one batch request.
  const transcribeBatch = useCallback(async () => {
    if (!connectionId || selectedCallIds.size === 0) return;
    setAdding(true);
    setError(null);
    setBatchResult(null);

    try {
      const response = await queueApi.add(connectionId, Array.from(selectedCallIds));
      const { queued, skipped_active, skipped_expired } = response;
      const totalSkipped = skipped_active.length + skipped_expired.length;

      if (queued > 0) {
        setBatchResult(`Добавлено: ${queued}`);
        setSelectedCallIds(new Set());
      }

      if (totalSkipped > 0) {
        const reasons = [];
        if (skipped_active.length) reasons.push(`${skipped_active.length} уже в очереди`);
        if (skipped_expired.length) reasons.push(`${skipped_expired.length} устарели`);
        const msg = `Добавлено: ${queued}. Пропущено: ${totalSkipped} (${reasons.join(", ")})`;
        setBatchResult(msg);
        if (queued === 0) {
          setError({ message: msg, code: "ALL_SKIPPED" });
        }
      }
    } catch (err) {
      setError(err);
    } finally {
      setAdding(false);
    }
  }, [connectionId, selectedCallIds]);

  // Selection helpers.
  const toggleSelection = useCallback(
    (callId) => {
      toggleId(setSelectedCallIds, callId);
    },
    [toggleId],
  );

  const toggleSelectAll = useCallback(() => {
    if (selectedCallIds.size === calls.length) {
      setSelectedCallIds(new Set());
    } else {
      setSelectedCallIds(new Set(calls.map((call) => call.id)));
    }
  }, [selectedCallIds, calls]);

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
        if (transcript_text) {
          updateCall(call_id, { transcript: transcript_text });
        }
        setQueueMap((prev) => ({ ...prev, [call_id]: status }));
      }
    });
  }, [subscribe, updateCall]);

  // Format the persisted timestamp for human consumption.
  const searchedLabel = useMemo(() => {
    if (!searchedAt) return null;
    const date = new Date(searchedAt);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }, [searchedAt]);

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
              {searchedLabel && (
                <div className="mb-4 text-xs text-slate-500">
                  Результаты загружены в {searchedLabel}
                </div>
              )}

              <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-slate-200 bg-slate-50 px-4 py-3">
                <p className="text-sm text-slate-600">
                  Выбрано: <span className="font-medium">{selectedCallIds.size}</span>
                  {selectedCallIds.size > 0 && (
                    <button
                      type="button"
                      onClick={() => setSelectedCallIds(new Set())}
                      className="ml-3 text-xs font-medium text-slate-500 underline"
                    >
                      Сбросить
                    </button>
                  )}
                </p>

                <div className="flex flex-wrap items-center gap-2">
                  {batchResult && (
                    <span className="text-sm text-slate-600">{batchResult}</span>
                  )}
                  <Link to="/queue">
                    <Button variant="secondary">Перейти в очередь</Button>
                  </Link>
                  <Button
                    onClick={transcribeBatch}
                    disabled={adding || selectedCallIds.size === 0}
                  >
                    {adding
                      ? "Добавление..."
                      : `Транскрибировать выбранные (${selectedCallIds.size})`}
                  </Button>
                </div>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-3 py-3">#</th>
                      <th className="px-3 py-3">
                        <input
                          type="checkbox"
                          checked={calls.length > 0 && selectedCallIds.size === calls.length}
                          onChange={toggleSelectAll}
                          className="h-4 w-4 rounded border-slate-300"
                          aria-label="Выбрать все отображённые звонки"
                        />
                      </th>
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
                        audioOpen={expandedAudioIds.has(call.id)}
                        transcriptOpen={expandedTranscriptIds.has(call.id)}
                        onToggleAudio={() => toggleId(setExpandedAudioIds, call.id)}
                        onToggleTranscript={() =>
                          toggleId(setExpandedTranscriptIds, call.id)
                        }
                        onTranscribe={transcribe}
                        onError={setError}
                        busy={adding}
                        selected={selectedCallIds.has(call.id)}
                        onToggleSelection={toggleSelection}
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
