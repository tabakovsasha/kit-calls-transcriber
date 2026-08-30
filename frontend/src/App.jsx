import { Fragment, useEffect, useMemo, useRef, useState } from "react";

const getLocalDatetimeValue = (daysAgo = 0) => {
  const date = new Date();
  date.setDate(date.getDate() - daysAgo);
  const offsetMs = date.getTimezoneOffset() * 60000;
  return new Date(date - offsetMs).toISOString().slice(0, 16);
};

const toLocalDatetimeInputValue = (date) => {
  const offsetMs = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
};

const getLocalDatetimeNowValue = () => toLocalDatetimeInputValue(new Date());

const getLocalDatetimeStartOfDayValue = () => {
  const date = new Date();
  date.setHours(0, 0, 0, 0);
  return toLocalDatetimeInputValue(date);
};

const AUTH_STORAGE_KEY = "call-history-auth";
const APP_STORAGE_KEY = "call-history-app-state";
const TRANSCRIBE_REQUEST_TIMEOUT_MS = 8 * 60 * 1000;
const DEFAULT_CALL_TIMEZONE = "Asia/Almaty";
const WEEKDAY_OPTIONS = [
  { value: 0, label: "Пн" },
  { value: 1, label: "Вт" },
  { value: 2, label: "Ср" },
  { value: 3, label: "Чт" },
  { value: 4, label: "Пт" },
  { value: 5, label: "Сб" },
  { value: 6, label: "Вс" },
];

const getSavedAuth = () => {
  if (typeof window === "undefined") {
    return { api_host: "", domain: "", access_token: "", scenario_name: "", scenario_id: null };
  }

  try {
    const saved = window.localStorage.getItem(AUTH_STORAGE_KEY);
    if (!saved) {
      return { api_host: "", domain: "", access_token: "", scenario_name: "", scenario_id: null };
    }
    const parsed = JSON.parse(saved);
    return {
      api_host: parsed.api_host || "",
      domain: parsed.domain || "",
      access_token: parsed.access_token || "",
      scenario_name: parsed.scenario_name || "",
      scenario_id: parsed.scenario_id || null,
    };
  } catch (error) {
    return { api_host: "", domain: "", access_token: "", scenario_name: "", scenario_id: null };
  }
};

const getSavedAppState = () => {
  if (typeof window === "undefined") {
    return {};
  }

  try {
    const saved = window.localStorage.getItem(APP_STORAGE_KEY);
    if (!saved) {
      return {};
    }
    return JSON.parse(saved);
  } catch (error) {
    return {};
  }
};

const getCallTimezone = (record) => {
  const value = record?.timezone || record?.time_zone || record?.tz;
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  return DEFAULT_CALL_TIMEZONE;
};

const normalizeRecordId = (value) => {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value);
};

const normalizeRecord = (record) => {
  if (!record || typeof record !== "object") {
    return record;
  }
  return {
    ...record,
    id: normalizeRecordId(record.id),
  };
};

const formatCallDateTime = (record) => {
  const raw = record?.datetime_start;
  if (!raw) {
    return "—";
  }

  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) {
    return String(raw);
  }

  const timezone = getCallTimezone(record);
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(parsed);
  } catch (error) {
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone: DEFAULT_CALL_TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(parsed);
  }
};

const initialFilters = {
  ...getSavedAuth(),
  from_date: getLocalDatetimeValue(1),
  to_date: getLocalDatetimeValue(),
  min_duration: 0,
  has_recording: true,
};

function App() {
  const savedAppState = useMemo(() => getSavedAppState(), []);
  const [filters, setFilters] = useState(savedAppState.filters || initialFilters);
  const [statusMessages, setStatusMessages] = useState([]);
  const [records, setRecords] = useState((savedAppState.records || []).map(normalizeRecord));
  const [transcribedHistoryRecords, setTranscribedHistoryRecords] = useState(
    (savedAppState.transcribedHistoryRecords || (savedAppState.records || []).filter((record) => (record.transcript || "").trim().length > 0)).map(normalizeRecord)
  );
  const [scenarios, setScenarios] = useState(savedAppState.scenarios || []);
  const [isScenarioListOpen, setIsScenarioListOpen] = useState(false);
  const [scenarioFilter, setScenarioFilter] = useState(savedAppState.scenarioFilter || "");
  const [processing, setProcessing] = useState(false);
  const [loadingScenarios, setLoadingScenarios] = useState(false);
  const [transcribingIds, setTranscribingIds] = useState([]);
  const [whisperModels, setWhisperModels] = useState([]);
  const [performanceDiagnostics, setPerformanceDiagnostics] = useState(null);
  const [selectedPerformanceProfile, setSelectedPerformanceProfile] = useState(savedAppState.selectedPerformanceProfile || "moderate");
  const [updatingPerformanceProfile, setUpdatingPerformanceProfile] = useState(false);
  const [selectedWhisperModel, setSelectedWhisperModel] = useState(savedAppState.selectedWhisperModel || "base");
  const [downloadingModel, setDownloadingModel] = useState(null);
  const [playingAudioId, setPlayingAudioId] = useState(null);
  const [activeAudioPanelId, setActiveAudioPanelId] = useState(null);
  const [audioProgressById, setAudioProgressById] = useState({});
  const [bulkTranscribingInProgress, setBulkTranscribingInProgress] = useState(false);
  const [bulkTranscribingProgress, setBulkTranscribingProgress] = useState({ current: 0, total: 0 });
  const [recordsLimit, setRecordsLimit] = useState(savedAppState.recordsLimit || 100);
  const [canLoadMore, setCanLoadMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [preparingAudioIds, setPreparingAudioIds] = useState([]);
  const [durationSortDirection, setDurationSortDirection] = useState(savedAppState.durationSortDirection || "");
  const [favoriteIds, setFavoriteIds] = useState(new Set((savedAppState.favoriteIds || []).map(normalizeRecordId)));
  const [favoriteComments, setFavoriteComments] = useState(savedAppState.favoriteComments || {});
  const [favoriteCommentDrafts, setFavoriteCommentDrafts] = useState(savedAppState.favoriteComments || {});
  const [openFavoriteCommentId, setOpenFavoriteCommentId] = useState(null);
  const [showFavoritesOnly, setShowFavoritesOnly] = useState(savedAppState.showFavoritesOnly || false);
  const [activeCallsTab, setActiveCallsTab] = useState(savedAppState.activeCallsTab || "all");
  const [transcribedScenarioFilter, setTranscribedScenarioFilter] = useState(savedAppState.transcribedScenarioFilter || "all");
  const [transcribedDatePreset, setTranscribedDatePreset] = useState(savedAppState.transcribedDatePreset || "all");
  const [transcribedDateFrom, setTranscribedDateFrom] = useState(savedAppState.transcribedDateFrom || "");
  const [transcribedDateTo, setTranscribedDateTo] = useState(savedAppState.transcribedDateTo || "");
  const [downloadingExport, setDownloadingExport] = useState(false);
  const [downloadingAudioIds, setDownloadingAudioIds] = useState([]);
  const [schedules, setSchedules] = useState([]);
  const [loadingSchedules, setLoadingSchedules] = useState(false);
  const [creatingSchedule, setCreatingSchedule] = useState(false);
  const [queueItems, setQueueItems] = useState([]);
  const [queueCounters, setQueueCounters] = useState({ total: 0, queued: 0, processing: 0, done: 0, failed: 0, skipped: 0, canceled: 0 });
  const [queueRunner, setQueueRunner] = useState({ running: false, stop_requested: false });
  const [loadingQueue, setLoadingQueue] = useState(false);
  const [queueBusyAction, setQueueBusyAction] = useState("");
  const [scheduleForm, setScheduleForm] = useState({
    name: "",
    weekdays: [1, 2, 3, 4, 5],
    from_hour: 9,
    to_hour: 18,
    min_duration: 0,
    has_recording: true,
  });
  const bulkTranscribingAbortRef = useRef(false);
  const wsRef = useRef(null);
  const wsConnectPromiseRef = useRef(null);
  const audioRefs = useRef({});
  const audioDownloadPromisesRef = useRef(new Map());
  const audioPrefetchAttemptedRef = useRef(new Set());
  const initialAudioPrefetchSkippedRef = useRef(false);
  const sessionId = useMemo(() => crypto.randomUUID(), []);

  useEffect(() => {
    const loadWhisperModels = async () => {
      try {
        const response = await fetch("http://localhost:8000/api/whisper/models");
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        setWhisperModels(data.models || []);
        if (data.default_model) {
          setSelectedWhisperModel((prev) => prev || data.default_model);
        }
      } catch (error) {
        console.warn("Не удалось загрузить список моделей Whisper", error);
      }
    };

    loadWhisperModels();
    const intervalId = window.setInterval(loadWhisperModels, 2000);

    return () => {
      window.clearInterval(intervalId);
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  useEffect(() => {
    const loadQueue = async () => {
      setLoadingQueue(true);
      try {
        const response = await fetch("http://localhost:8000/api/queue");
        if (!response.ok) {
          throw new Error(await response.text());
        }
        const data = await response.json();
        setQueueItems(data.items || []);
        setQueueCounters(data.counters || { total: 0, queued: 0, processing: 0, done: 0, failed: 0, skipped: 0, canceled: 0 });
        setQueueRunner(data.runner || { running: false, stop_requested: false });
      } catch (error) {
        addStatus(`[ERROR] Не удалось загрузить очередь транскрибации: ${error.message}`);
      } finally {
        setLoadingQueue(false);
      }
    };

    loadQueue();
    void openWebSocket().catch(() => {
      addStatus("[WARN] Не удалось подключить WebSocket для live-обновлений очереди.");
    });
  }, []);

  useEffect(() => {
    const loadPerformanceDiagnostics = async () => {
      try {
        const response = await fetch("http://localhost:8000/api/performance/diagnostics");
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        setPerformanceDiagnostics(data);
        if (data.performance_profile?.active_profile) {
          setSelectedPerformanceProfile(data.performance_profile.active_profile);
        }
      } catch (error) {
        console.warn("Не удалось загрузить диагностику производительности", error);
      }
    };

    loadPerformanceDiagnostics();
    const intervalId = window.setInterval(loadPerformanceDiagnostics, 3000);
    return () => window.clearInterval(intervalId);
  }, []);

  useEffect(() => {
    const loadSchedules = async () => {
      setLoadingSchedules(true);
      try {
        const response = await fetch("http://localhost:8000/api/schedules");
        if (!response.ok) {
          throw new Error(await response.text());
        }
        const data = await response.json();
        setSchedules(data.schedules || []);
      } catch (error) {
        addStatus(`[ERROR] Не удалось загрузить расписания: ${error.message}`);
      } finally {
        setLoadingSchedules(false);
      }
    };

    loadSchedules();
  }, []);

  const addStatus = (text) => {
    setStatusMessages((prev) => [...prev, `${new Date().toLocaleTimeString()}: ${text}`]);
  };

  const upsertRecord = (previousRecords, incomingRecord) => {
    const normalizedIncomingRecord = normalizeRecord(incomingRecord);
    const incomingRecordId = normalizeRecordId(normalizedIncomingRecord?.id);
    const existingIndex = previousRecords.findIndex((item) => normalizeRecordId(item.id) === incomingRecordId);
    if (existingIndex === -1) {
      return [normalizedIncomingRecord, ...previousRecords];
    }

    return previousRecords.map((item) => {
      if (normalizeRecordId(item.id) !== incomingRecordId) {
        return item;
      }

      return {
        ...item,
        ...normalizedIncomingRecord,
        transcript: normalizedIncomingRecord.transcript ?? item.transcript,
        local_audio_url: normalizedIncomingRecord.local_audio_url ?? item.local_audio_url,
      };
    });
  };

  const upsertTranscribedHistory = (incomingRecord) => {
    if (!(incomingRecord?.transcript || "").trim()) {
      return;
    }

    setTranscribedHistoryRecords((previousRecords) => upsertRecord(previousRecords, incomingRecord));
  };

  const prepareAudioSource = async (record, { markAsPreparing = false } = {}) => {
    if (!record?.id) {
      throw new Error("Не удалось определить звонок для подготовки аудио.");
    }

    if (record.local_audio_url) {
      return record.local_audio_url;
    }

    if (!record.record_url) {
      throw new Error("У звонка нет ссылки на аудио.");
    }

    const existingPromise = audioDownloadPromisesRef.current.get(record.id);
    if (existingPromise) {
      if (markAsPreparing) {
        setPreparingAudioIds((prev) => (prev.includes(record.id) ? prev : [...prev, record.id]));
      }

      try {
        return await existingPromise;
      } finally {
        if (markAsPreparing) {
          setPreparingAudioIds((prev) => prev.filter((id) => id !== record.id));
        }
      }
    }

    const downloadPromise = (async () => {
      const response = await fetch("http://localhost:8000/api/audio/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: record.id,
          record_url: record.record_url,
          access_token: filters.access_token,
          api_host: filters.api_host,
          domain: filters.domain,
        }),
      });

      if (!response.ok) {
        throw new Error(await response.text());
      }

      const data = await response.json();
      const audioUrl = data.audio_url;
      setRecords((prev) =>
        upsertRecord(prev, {
          id: record.id,
          local_audio_url: audioUrl,
        })
      );
      return audioUrl;
    })();

    audioDownloadPromisesRef.current.set(record.id, downloadPromise);
    if (markAsPreparing) {
      setPreparingAudioIds((prev) => (prev.includes(record.id) ? prev : [...prev, record.id]));
    }

    try {
      return await downloadPromise;
    } finally {
      audioDownloadPromisesRef.current.delete(record.id);
      if (markAsPreparing) {
        setPreparingAudioIds((prev) => prev.filter((id) => id !== record.id));
      }
    }
  };

  useEffect(() => {
    if (!initialAudioPrefetchSkippedRef.current) {
      initialAudioPrefetchSkippedRef.current = true;
      return;
    }

    const recordsToPrefetch = records
      .filter((record) => record.record_url && !record.local_audio_url && !audioPrefetchAttemptedRef.current.has(record.id))
      .slice(0, 8);

    if (recordsToPrefetch.length === 0) {
      return;
    }

    recordsToPrefetch.forEach((record) => {
      audioPrefetchAttemptedRef.current.add(record.id);
      void prepareAudioSource(record).catch((error) => {
        console.warn(`[AUDIO] Prefetch failed for ${record.id}:`, error);
      });
    });
  }, [records]);

  const handleWsMessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "status") {
      addStatus(message.message);
      if (message.message.includes("Готово!")) {
        setProcessing(false);
      }
    }
    if (message.type === "record" || message.type === "call") {
      const incomingRecord = message.call || message.record;
      setRecords((prev) => upsertRecord(prev, incomingRecord));
      upsertTranscribedHistory(incomingRecord);
    }
    if (message.type === "pagination_status") {
      setCanLoadMore(message.can_load_more);
    }
    if (message.type === "queue_snapshot") {
      setQueueItems(message.items || []);
      setQueueCounters(message.counters || { total: 0, queued: 0, processing: 0, done: 0, failed: 0, skipped: 0, canceled: 0 });
      setQueueRunner(message.runner || { running: false, stop_requested: false });
    }
  };

  const openWebSocket = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      return Promise.resolve(wsRef.current);
    }
    if (wsConnectPromiseRef.current) {
      return wsConnectPromiseRef.current;
    }

    wsConnectPromiseRef.current = new Promise((resolve, reject) => {
      const ws = new WebSocket(`ws://localhost:8000/ws?session_id=${sessionId}`);
      ws.onopen = () => {
        wsRef.current = ws;
        wsConnectPromiseRef.current = null;
        resolve(ws);
      };
      ws.onmessage = handleWsMessage;
      ws.onerror = (event) => {
        wsConnectPromiseRef.current = null;
        reject(event);
      };
      ws.onclose = () => {
        wsRef.current = null;
      };
    });

    return wsConnectPromiseRef.current;
  };

  const callQueueApi = async (url, options = {}, successStatusText = "") => {
    setQueueBusyAction(url);
    try {
      const response = await fetch(url, options);
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = await response.json();
      if (data.items) {
        setQueueItems(data.items || []);
      }
      if (data.counters) {
        setQueueCounters(data.counters || { total: 0, queued: 0, processing: 0, done: 0, failed: 0, skipped: 0, canceled: 0 });
      }
      if (data.runner) {
        setQueueRunner(data.runner || { running: false, stop_requested: false });
      }
      if (successStatusText) {
        addStatus(successStatusText);
      }
      return data;
    } finally {
      setQueueBusyAction("");
    }
  };

  const handleAddToQueue = async (record) => {
    if (!record?.record_url) {
      addStatus(`[ERROR] Нельзя добавить звонок ${record?.id || ""} в очередь: отсутствует запись.`);
      return;
    }
    try {
      const data = await callQueueApi(
        "http://localhost:8000/api/queue/add",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            record,
            api_host: filters.api_host,
            domain: filters.domain,
            access_token: filters.access_token,
            whisper_model: selectedWhisperModel,
          }),
        }
      );
      if (data.status === "exists") {
        addStatus(`[INFO] Звонок ${record.id} уже есть в очереди.`);
      } else {
        addStatus(`[OK] Звонок ${record.id} добавлен в очередь.`);
      }
    } catch (error) {
      addStatus(`[ERROR] Не удалось добавить звонок ${record.id} в очередь: ${error.message}`);
    }
  };

  const handleAddVisibleToQueue = async () => {
    const source = visibleRecords.filter((record) => record.record_url);
    if (source.length === 0) {
      addStatus("[INFO] Нет записей с аудио для добавления в очередь.");
      return;
    }
    try {
      const data = await callQueueApi(
        "http://localhost:8000/api/queue/add-bulk",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            records: source,
            api_host: filters.api_host,
            domain: filters.domain,
            access_token: filters.access_token,
            whisper_model: selectedWhisperModel,
          }),
        }
      );
      addStatus(`[OK] В очередь добавлено: ${data.added || 0}, уже существовали: ${data.existed || 0}.`);
    } catch (error) {
      addStatus(`[ERROR] Не удалось добавить список в очередь: ${error.message}`);
    }
  };

  const handleStartQueue = async () => {
    try {
      const data = await callQueueApi(
        "http://localhost:8000/api/queue/start",
        { method: "POST" }
      );
      if (data.status === "already_running") {
        addStatus("[INFO] Очередь уже обрабатывается.");
      } else if (data.status === "nothing_to_process") {
        addStatus("[INFO] В очереди нет звонков со статусом queued.");
      } else {
        addStatus("[OK] Запущена транскрибация всей очереди.");
      }
    } catch (error) {
      addStatus(`[ERROR] Не удалось запустить очередь: ${error.message}`);
    }
  };

  const handleStopQueue = async () => {
    try {
      await callQueueApi(
        "http://localhost:8000/api/queue/stop",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cancel_queued: false }),
        },
        "[OK] Остановка очереди запрошена."
      );
    } catch (error) {
      addStatus(`[ERROR] Не удалось остановить очередь: ${error.message}`);
    }
  };

  const handleRetryFailedQueue = async () => {
    try {
      const data = await callQueueApi(
        "http://localhost:8000/api/queue/retry-failed",
        { method: "POST" }
      );
      addStatus(`[OK] Повторно поставлено в очередь ошибок: ${data.retried || 0}.`);
    } catch (error) {
      addStatus(`[ERROR] Не удалось повторить failed: ${error.message}`);
    }
  };

  const handleRetrySkippedQueue = async () => {
    try {
      const data = await callQueueApi(
        "http://localhost:8000/api/queue/retry-skipped",
        { method: "POST" }
      );
      addStatus(`[OK] Повторно поставлено в очередь skipped: ${data.retried || 0}.`);
    } catch (error) {
      addStatus(`[ERROR] Не удалось повторить skipped: ${error.message}`);
    }
  };

  const handleClearQueueByStatuses = async (statuses) => {
    try {
      const data = await callQueueApi(
        "http://localhost:8000/api/queue/clear",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ statuses }),
        }
      );
      addStatus(`[OK] Из очереди удалено записей: ${data.removed || 0}.`);
    } catch (error) {
      addStatus(`[ERROR] Не удалось очистить очередь: ${error.message}`);
    }
  };

  const handleDeleteQueueItem = async (callId) => {
    try {
      await callQueueApi(
        `http://localhost:8000/api/queue/${callId}`,
        { method: "DELETE" },
        `[OK] Удален элемент очереди ${callId}.`
      );
    } catch (error) {
      addStatus(`[ERROR] Не удалось удалить элемент ${callId}: ${error.message}`);
    }
  };

  const handlePerformanceProfileChange = async (nextProfile) => {
    const previous = selectedPerformanceProfile;
    setSelectedPerformanceProfile(nextProfile);
    setUpdatingPerformanceProfile(true);
    try {
      const response = await fetch("http://localhost:8000/api/performance/profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: nextProfile }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }

      const data = await response.json();
      addStatus(
        data.pending_torch_update
          ? "[INFO] Профиль применен. Потоки PyTorch обновятся после завершения активных транскрибаций."
          : "[OK] Профиль производительности применен сразу."
      );
      if (data.profile) {
        setSelectedPerformanceProfile(data.profile);
      }
    } catch (error) {
      setSelectedPerformanceProfile(previous);
      addStatus(`[ERROR] Не удалось обновить профиль производительности: ${error.message}`);
    } finally {
      setUpdatingPerformanceProfile(false);
    }
  };

  const saveAuth = (partial) => {
    setFilters((prev) => ({ ...prev, ...partial }));
  };

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const stateToSave = {
      filters,
      records,
      transcribedHistoryRecords,
      scenarios,
      scenarioFilter,
      selectedPerformanceProfile,
      selectedWhisperModel,
      recordsLimit,
      favoriteIds: Array.from(favoriteIds),
      favoriteComments,
      showFavoritesOnly,
      activeCallsTab,
      durationSortDirection,
      transcribedScenarioFilter,
      transcribedDatePreset,
      transcribedDateFrom,
      transcribedDateTo,
    };

    try {
      window.localStorage.setItem(APP_STORAGE_KEY, JSON.stringify(stateToSave));
    } catch (error) {
      console.warn("Не удалось сохранить состояние приложения", error);
    }
  }, [filters, records, transcribedHistoryRecords, scenarios, scenarioFilter, selectedPerformanceProfile, selectedWhisperModel, recordsLimit, favoriteIds, favoriteComments, showFavoritesOnly, activeCallsTab, transcribedScenarioFilter, transcribedDatePreset, transcribedDateFrom, transcribedDateTo]);

  const handleFetchScenarios = async () => {
    setLoadingScenarios(true);
    try {
      const params = new URLSearchParams({
        api_host: filters.api_host,
        domain: filters.domain,
        access_token: filters.access_token,
      });
      const response = await fetch(`http://localhost:8000/api/scenarios?${params}`);
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = await response.json();
      setScenarios(data);
      setIsScenarioListOpen(true);
    } catch (error) {
      addStatus(`Ошибка загрузки сценариев: ${error.message}`);
    } finally {
      setLoadingScenarios(false);
    }
  };

  const handleScenarioSelect = (scenario) => {
    saveAuth({
      scenario_name: scenario.title,
      scenario_id: scenario.id,
    });
    setIsScenarioListOpen(false);
    setScenarioFilter("");
  };

  const handleDownloadWhisperModel = async (modelName) => {
    setDownloadingModel(modelName);
    try {
      const response = await fetch("http://localhost:8000/api/whisper/download-model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_name: modelName }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      addStatus(`[LOADING] Запущена загрузка модели Whisper: ${modelName}`);
    } catch (error) {
      addStatus(`[ERROR] Не удалось запустить загрузку модели ${modelName}: ${error.message}`);
    } finally {
      setDownloadingModel(null);
    }
  };

  const handleModelClick = (model) => {
    if (model.status === "loading") {
      return;
    }

    if (model.status === "ready") {
      setSelectedWhisperModel(model.name);
      addStatus(`[OK] Выбрана модель Whisper: ${model.name}`);
      return;
    }

    handleDownloadWhisperModel(model.name);
  };

  const handleTranscribe = async (record) => {
    if (!record.record_url) {
      addStatus(`Нет записи для звонка ${record.id}`);
      return;
    }

    console.log(`[TRANSCRIBE] Начало транскрибации для звонка ${record.id}`);
    console.log(`[TRANSCRIBE] URL аудио: ${record.record_url}`);
    console.log(`[TRANSCRIBE] Выбранная модель Whisper: ${selectedWhisperModel}`);
    addStatus(`[LOADING] Транскрибация: загрузка аудио...`);
    setTranscribingIds((prev) => [...prev, record.id]);
    let timeoutId;

    try {
      console.log(`[TRANSCRIBE] Отправляю запрос на сервер...`);
      addStatus(`[LOADING] Транскрибация: обработка на сервере (это может занять время)...`);

      const controller = new AbortController();
      timeoutId = window.setTimeout(() => controller.abort(), TRANSCRIBE_REQUEST_TIMEOUT_MS);

      const response = await fetch("http://localhost:8000/api/transcribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          id: record.id,
          record_url: record.record_url,
          whisper_model: selectedWhisperModel,
          access_token: filters.access_token,
          api_host: filters.api_host,
          domain: filters.domain,
        }),
      });

      console.log(`[TRANSCRIBE] Ответ от сервера: ${response.status}`);

      if (!response.ok) {
        const text = await response.text();
        console.error(`[TRANSCRIBE] Ошибка сервера: ${text}`);
        throw new Error(text || "Ошибка транскрибации");
      }

      const data = await response.json();
      console.log(`[TRANSCRIBE] Полный ответ сервера:`, data);
      console.log(`[TRANSCRIBE] Получен результат: ${data.transcript.length} символов`);
      console.log(`[TRANSCRIBE] Первые 200 символов: "${data.transcript.substring(0, 200)}"`);

      setRecords((prev) =>
        upsertRecord(prev, {
          id: data.id,
          transcript: data.transcript,
          local_audio_url: data.audio_url,
        })
      );
      upsertTranscribedHistory({
        id: data.id,
        transcript: data.transcript,
        local_audio_url: data.audio_url,
        record_url: record.record_url,
        datetime_start: record.datetime_start,
        timezone: record.timezone,
        phone_a: record.phone_a,
        phone_b: record.phone_b,
        duration: record.duration,
        scenario: record.scenario,
        scenario_name: record.scenario_name || record.scenario?.title || record.scenario?.name,
      });

      console.log(`[TRANSCRIBE] OK - Успешно!`);
      addStatus(`[OK] Транскрибация готова для звонка ${record.id} (${data.transcript.length} символов)`);
    } catch (error) {
      console.error(`[TRANSCRIBE] ERROR:`, error);
      if (error?.name === "AbortError") {
        addStatus(`[ERROR] Превышено время ожидания транскрибации для звонка ${record.id}. Запрос отменен.`);
      } else {
        addStatus(`[ERROR] Ошибка транскрибации: ${error.message}`);
      }
    } finally {
      if (timeoutId) {
        window.clearTimeout(timeoutId);
      }
      setTranscribingIds((prev) => prev.filter((id) => id !== record.id));
    }
  };

  const toggleAudioPlayback = async (record) => {
    if (!record.record_url && !record.local_audio_url) {
      console.warn("[AUDIO] No URL for record", record.id);
      return;
    }

    const audioEl = audioRefs.current[record.id];
    if (!audioEl) {
      console.warn("[AUDIO] Audio element not found for record", record.id);
      return;
    }

    setActiveAudioPanelId(record.id);

    if (playingAudioId === record.id) {
      audioEl.pause();
      setPlayingAudioId(null);
      return;
    }

    Object.values(audioRefs.current).forEach((element) => {
      if (element && !element.paused) {
        element.pause();
      }
    });
    setPlayingAudioId(null);

    try {
      const audioUrl = await prepareAudioSource(record, { markAsPreparing: true });
      console.log("[AUDIO] Using prepared URL:", audioUrl);
      if (!audioEl.src || audioEl.src !== audioUrl) {
        audioEl.src = audioUrl;
        audioEl.currentTime = 0;
      }

      console.log("[AUDIO] Audio element src set to:", audioEl.src);
      console.log("[AUDIO] Playing...");
      await audioEl.play();
      setPlayingAudioId(record.id);
    } catch (error) {
      console.error("[AUDIO] Failed to play audio", error);
      setPlayingAudioId(null);
      setActiveAudioPanelId(null);
    }
  };

  const handleDownloadAudio = async (record) => {
    if (!record.record_url && !record.local_audio_url) {
      addStatus(`[ERROR] У звонка ${record.id} нет доступного аудио для скачивания.`);
      return;
    }

    setDownloadingAudioIds((prev) => [...prev, record.id]);

    try {
      let audioUrl = record.local_audio_url;
      if (!audioUrl && record.record_url) {
        const response = await fetch("http://localhost:8000/api/audio/download", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: record.id,
            record_url: record.record_url,
            access_token: filters.access_token,
            api_host: filters.api_host,
            domain: filters.domain,
          }),
        });

        if (!response.ok) {
          throw new Error(await response.text());
        }

        const data = await response.json();
        audioUrl = data.audio_url;
        setRecords((prev) =>
          upsertRecord(prev, {
            id: record.id,
            local_audio_url: data.audio_url,
          })
        );
      }

      if (!audioUrl) {
        throw new Error("Не удалось подготовить ссылку на аудио.");
      }

      const sourcePath = record.record_url || audioUrl;
      const extension = sourcePath.includes(".") ? sourcePath.split(".").pop()?.split("?")[0] : "mp3";
      const anchor = document.createElement("a");
      anchor.href = audioUrl;
      anchor.download = `call-${record.id}.${extension || "mp3"}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      addStatus(`[OK] Аудио звонка ${record.id} скачивается.`);
    } catch (error) {
      addStatus(`[ERROR] Не удалось скачать аудио звонка ${record.id}: ${error.message}`);
    } finally {
      setDownloadingAudioIds((prev) => prev.filter((id) => id !== record.id));
    }
  };

  const updateAudioProgress = (recordId, audioEl) => {
    const duration = Number.isFinite(audioEl.duration) ? audioEl.duration : 0;
    const currentTime = Number.isFinite(audioEl.currentTime) ? audioEl.currentTime : 0;
    setAudioProgressById((prev) => ({
      ...prev,
      [recordId]: {
        duration,
        currentTime,
      },
    }));
  };

  const handleAudioTimeUpdate = (recordId, event) => {
    updateAudioProgress(recordId, event.currentTarget);
  };

  const handleAudioMetadataLoaded = (recordId, event) => {
    updateAudioProgress(recordId, event.currentTarget);
  };

  const handleAudioSeek = (recordId, nextSeconds) => {
    const audioEl = audioRefs.current[recordId];
    if (!audioEl) {
      return;
    }

    const target = Number(nextSeconds);
    if (!Number.isFinite(target)) {
      return;
    }

    audioEl.currentTime = target;
    updateAudioProgress(recordId, audioEl);
  };

  const handleAudioEnded = (recordId) => {
    setPlayingAudioId((current) => (current === recordId ? null : current));
    const audioEl = audioRefs.current[recordId];
    if (audioEl) {
      updateAudioProgress(recordId, audioEl);
    }
  };

  const formatAudioTimelineTime = (value) => {
    const totalSeconds = Math.max(0, Math.floor(Number(value) || 0));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  };

  const handleBulkTranscribe = async () => {
    const recordsToTranscribe = records.filter((r) => r.record_url && !r.transcript);
    if (recordsToTranscribe.length === 0) {
      addStatus("[INFO] Все записи уже транскрибированы или не имеют аудио.");
      return;
    }

    setBulkTranscribingInProgress(true);
    bulkTranscribingAbortRef.current = false;
    setBulkTranscribingProgress({ current: 0, total: recordsToTranscribe.length });
    addStatus(`[LOADING] Начинаем массовую транскрибацию ${recordsToTranscribe.length} записей...`);

    const queue = [...recordsToTranscribe];
    let completed = 0;
    const hwConcurrency = typeof navigator !== "undefined" ? navigator.hardwareConcurrency || 4 : 4;
    const targetConcurrency = Math.max(2, Math.min(6, Math.floor(hwConcurrency / 3)));
    const concurrency = Math.min(targetConcurrency, queue.length);

    const worker = async () => {
      while (!bulkTranscribingAbortRef.current && queue.length > 0) {
        const record = queue.shift();
        if (!record) {
          break;
        }

        completed += 1;
        setBulkTranscribingProgress({ current: completed, total: recordsToTranscribe.length });

        try {
          await handleTranscribe(record);
        } catch (error) {
          console.error(`[BULK] Ошибка при транскрибации записи ${record.id}:`, error);
        }
      }
    };

    await Promise.all(Array.from({ length: concurrency }, () => worker()));

    setBulkTranscribingInProgress(false);
    setBulkTranscribingProgress({ current: 0, total: 0 });
    addStatus("[OK] Массовая транскрибация завершена.");
  };

  const handleCancelBulkTranscribe = () => {
    bulkTranscribingAbortRef.current = true;
  };

  const handleDownloadTranscriptsXlsx = async () => {
    setDownloadingExport(true);
    try {
      const response = await fetch("http://localhost:8000/api/transcripts/export-xlsx", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ records: transcribedRecords }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const blob = await response.blob();
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `transcripts-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.xlsx`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(objectUrl);
      addStatus("[OK] XLSX с транскриптами успешно скачан.");
    } catch (error) {
      addStatus(`[ERROR] Не удалось скачать XLSX: ${error.message}`);
    } finally {
      setDownloadingExport(false);
    }
  };

  const refreshSchedules = async () => {
    setLoadingSchedules(true);
    try {
      const response = await fetch("http://localhost:8000/api/schedules");
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = await response.json();
      setSchedules(data.schedules || []);
    } catch (error) {
      addStatus(`[ERROR] Не удалось обновить расписания: ${error.message}`);
    } finally {
      setLoadingSchedules(false);
    }
  };

  const toggleWeekday = (day) => {
    setScheduleForm((prev) => {
      const hasDay = prev.weekdays.includes(day);
      return {
        ...prev,
        weekdays: hasDay ? prev.weekdays.filter((item) => item !== day) : [...prev.weekdays, day],
      };
    });
  };

  const handleCreateSchedule = async () => {
    if (!filters.api_host || !filters.domain || !filters.access_token) {
      addStatus("[ERROR] Для расписания сначала заполните host/domain/access_token.");
      return;
    }
    if (scheduleForm.weekdays.length === 0) {
      addStatus("[ERROR] Выберите хотя бы один день недели для расписания.");
      return;
    }

    setCreatingSchedule(true);
    try {
      const response = await fetch("http://localhost:8000/api/schedules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: scheduleForm.name,
          weekdays: scheduleForm.weekdays,
          from_hour: Number(scheduleForm.from_hour),
          to_hour: Number(scheduleForm.to_hour),
          api_host: filters.api_host,
          domain: filters.domain,
          access_token: filters.access_token,
          scenario_id: filters.scenario_id,
          min_duration: Number(scheduleForm.min_duration),
          has_recording: Boolean(scheduleForm.has_recording),
          records_limit: recordsLimit,
          whisper_model: selectedWhisperModel,
          enabled: true,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      addStatus("[OK] Расписание создано. Загрузка и транскрибация будут выполняться в фоне на сервере.");
      await refreshSchedules();
    } catch (error) {
      addStatus(`[ERROR] Не удалось создать расписание: ${error.message}`);
    } finally {
      setCreatingSchedule(false);
    }
  };

  const handleToggleSchedule = async (schedule) => {
    try {
      const response = await fetch(`http://localhost:8000/api/schedules/${schedule.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !schedule.enabled }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      await refreshSchedules();
    } catch (error) {
      addStatus(`[ERROR] Не удалось изменить расписание: ${error.message}`);
    }
  };

  const handleDeleteSchedule = async (scheduleId) => {
    try {
      const response = await fetch(`http://localhost:8000/api/schedules/${scheduleId}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      await refreshSchedules();
      addStatus("[OK] Расписание удалено.");
    } catch (error) {
      addStatus(`[ERROR] Не удалось удалить расписание: ${error.message}`);
    }
  };

  const handleRunScheduleNow = async (scheduleId) => {
    try {
      const response = await fetch(`http://localhost:8000/api/schedules/${scheduleId}/run-now`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      addStatus("[OK] Запуск расписания инициирован вручную.");
    } catch (error) {
      addStatus(`[ERROR] Не удалось запустить расписание: ${error.message}`);
    }
  };

  const handleLoadMore = async () => {
    setLoadingMore(true);
    try {
      const response = await fetch("http://localhost:8000/api/load-more", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
        }),
      });
      if (!response.ok) {
        const error = await response.text();
        addStatus(`[ERROR] Не удалось загрузить дополнительные записи: ${error}`);
      }
    } catch (error) {
      addStatus(`[ERROR] Ошибка загрузки дополнительных записей: ${error.message}`);
    } finally {
      setLoadingMore(false);
    }
  };

  const filteredScenarios = scenarios.filter((scenario) =>
    scenario.title.toLowerCase().includes(scenarioFilter.toLowerCase())
  );

  const transcribedRecordsWithScenario = useMemo(() => {
    const recordsById = new Map(records.map((item) => [item.id, item]));
    return transcribedHistoryRecords.map((record) => {
      const sourceRecord = recordsById.get(record.id);
      if (!sourceRecord) {
        return record;
      }

      const hasDuration = Number.isFinite(Number(record.duration)) && Number(record.duration) > 0;
      const hasScenario = Boolean(record.scenario) || Boolean(record.scenario_name);

      if (hasDuration && hasScenario) {
        return record;
      }

      return {
        ...record,
        duration: hasDuration ? record.duration : (sourceRecord.duration ?? record.duration),
        scenario: hasScenario ? record.scenario : sourceRecord.scenario,
        scenario_name: hasScenario
          ? record.scenario_name
          : (sourceRecord.scenario_name || sourceRecord.scenario?.title || sourceRecord.scenario?.name),
        timezone: record.timezone || sourceRecord.timezone,
      };
    });
  }, [transcribedHistoryRecords, records]);

  const transcribedRecords = transcribedRecordsWithScenario;

  const getScenarioTitle = (record) => {
    if (!record) {
      return "";
    }
    if (typeof record.scenario === "string") {
      return record.scenario;
    }
    return record.scenario?.title || record.scenario?.name || record.scenario_name || "";
  };

  const availableTranscribedScenarios = useMemo(() => {
    const names = new Set();
    transcribedRecordsWithScenario.forEach((record) => {
      const title = getScenarioTitle(record).trim();
      if (title) {
        names.add(title);
      }
    });
    return Array.from(names).sort((a, b) => a.localeCompare(b, "ru"));
  }, [transcribedRecordsWithScenario]);

  useEffect(() => {
    if (transcribedScenarioFilter === "all") {
      return;
    }
    if (!availableTranscribedScenarios.includes(transcribedScenarioFilter)) {
      setTranscribedScenarioFilter("all");
    }
  }, [availableTranscribedScenarios, transcribedScenarioFilter]);

  useEffect(() => {
    if (transcribedDatePreset === "all") {
      setTranscribedDateFrom("");
      setTranscribedDateTo("");
      return;
    }

    if (transcribedDatePreset === "custom") {
      return;
    }

    if (transcribedDatePreset === "today") {
      setTranscribedDateFrom(getLocalDatetimeStartOfDayValue());
      setTranscribedDateTo(getLocalDatetimeNowValue());
      return;
    }

    const now = new Date();
    const from = new Date(now);
    if (transcribedDatePreset === "24h") {
      from.setHours(from.getHours() - 24);
    } else if (transcribedDatePreset === "7d") {
      from.setDate(from.getDate() - 7);
    } else if (transcribedDatePreset === "30d") {
      from.setDate(from.getDate() - 30);
    }

    setTranscribedDateFrom(toLocalDatetimeInputValue(from));
    setTranscribedDateTo(toLocalDatetimeInputValue(now));
  }, [transcribedDatePreset]);

  const filteredTranscribedByScenario = transcribedScenarioFilter !== "all"
    ? transcribedRecordsWithScenario.filter((record) => getScenarioTitle(record) === transcribedScenarioFilter)
    : transcribedRecordsWithScenario;

  const filteredTranscribedByDate = filteredTranscribedByScenario.filter((record) => {
    if (!transcribedDateFrom && !transcribedDateTo) {
      return true;
    }

    const callDate = record?.datetime_start ? new Date(record.datetime_start) : null;
    if (!callDate || Number.isNaN(callDate.getTime())) {
      return false;
    }

    if (transcribedDateFrom) {
      const fromDate = new Date(transcribedDateFrom);
      if (!Number.isNaN(fromDate.getTime()) && callDate < fromDate) {
        return false;
      }
    }

    if (transcribedDateTo) {
      const toDate = new Date(transcribedDateTo);
      if (!Number.isNaN(toDate.getTime()) && callDate > toDate) {
        return false;
      }
    }

    return true;
  });

  const getRecordDurationSeconds = (record) => {
    const duration = Number(record?.duration);
    return Number.isFinite(duration) && duration >= 0 ? duration : null;
  };

  const queueByCallId = useMemo(() => {
    const map = new Map();
    queueItems.forEach((item) => {
      map.set(normalizeRecordId(item.call_id), item);
    });
    return map;
  }, [queueItems]);

  const recordsByTab = activeCallsTab === "transcribed"
    ? filteredTranscribedByDate
    : activeCallsTab === "queue"
      ? queueItems.map((item) => ({
        id: normalizeRecordId(item.call_id),
        datetime_start: item.datetime_start,
        caller_a: item.caller_a,
        caller_b: item.caller_b,
        duration: item.duration,
        transcript: item.transcript_text,
        queue_status: item.status,
        queue_error_message: item.error_message,
      }))
      : records;
  const visibleRecords = useMemo(() => {
    const baseRecords = showFavoritesOnly
      ? recordsByTab.filter((record) => favoriteIds.has(record.id))
      : recordsByTab;

    if (!durationSortDirection) {
      return baseRecords;
    }

    return [...baseRecords].sort((left, right) => {
      const leftDuration = getRecordDurationSeconds(left);
      const rightDuration = getRecordDurationSeconds(right);

      if (leftDuration === null && rightDuration === null) {
        return 0;
      }
      if (leftDuration === null) {
        return 1;
      }
      if (rightDuration === null) {
        return -1;
      }

      if (leftDuration === rightDuration) {
        return 0;
      }

      return durationSortDirection === "asc" ? leftDuration - rightDuration : rightDuration - leftDuration;
    });
  }, [recordsByTab, showFavoritesOnly, favoriteIds, durationSortDirection]);

  const toggleDurationSort = () => {
    setDurationSortDirection((current) => {
      if (current === "asc") {
        return "desc";
      }
      if (current === "desc") {
        return "";
      }
      return "asc";
    });
  };

  const toggleFavorite = (recordId) => {
    setFavoriteIds((prev) => {
      const next = new Set(prev);
      const isFavorite = next.has(recordId);
      if (isFavorite) {
        next.delete(recordId);
        setOpenFavoriteCommentId((current) => (current === recordId ? null : current));
      } else {
        next.add(recordId);
        setFavoriteCommentDrafts((drafts) => ({
          ...drafts,
          [recordId]: drafts[recordId] ?? favoriteComments[recordId] ?? "",
        }));
        setOpenFavoriteCommentId(recordId);
      }
      return next;
    });
  };

  const handleFavoriteCommentDraftChange = (recordId, value) => {
    setFavoriteCommentDrafts((prev) => ({
      ...prev,
      [recordId]: value,
    }));
  };

  const handleSaveFavoriteComment = (recordId) => {
    const draft = (favoriteCommentDrafts[recordId] || "").trim();
    setFavoriteComments((prev) => ({
      ...prev,
      [recordId]: draft,
    }));
    addStatus(draft ? `[OK] Комментарий сохранён для звонка ${recordId}.` : `[OK] Комментарий очищен для звонка ${recordId}.`);
    setOpenFavoriteCommentId(null);
  };

  const durationSortLabel = durationSortDirection === "asc"
    ? "↑"
    : durationSortDirection === "desc"
      ? "↓"
      : "↕";

  const handleSubmit = async (event) => {
    event.preventDefault();
    setRecords([]);
    setStatusMessages([]);
    setProcessing(true);
    setCanLoadMore(false);

    try {
      await openWebSocket();
      await fetch("http://localhost:8000/api/process", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_host: filters.api_host,
          domain: filters.domain,
          access_token: filters.access_token,
          from_date: filters.from_date,
          to_date: filters.to_date,
          scenario_name: filters.scenario_name,
          scenario_id: filters.scenario_id,
          min_duration: filters.min_duration,
          has_recording: filters.has_recording,
          session_id: sessionId,
          records_limit: recordsLimit,
        }),
      });
    } catch (error) {
      addStatus("Не удалось подключиться к серверу.");
      setProcessing(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 px-4 py-8">
      <div className="mx-auto max-w-6xl space-y-8">
        <header className="rounded-3xl bg-white/80 p-8 shadow-xl shadow-slate-200/80 backdrop-blur">
          <h1 className="text-3xl font-semibold tracking-tight">Voximplant Call Transcription Service</h1>
          <p className="mt-2 max-w-2xl text-slate-600">Загружайте историю звонков, транскрибируйте записи и просматривайте результаты в реальном времени.</p>
        </header>

        <form onSubmit={handleSubmit} className="rounded-3xl bg-white p-6 shadow-xl shadow-slate-200/80">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 pb-4">
            <div>
              <h2 className="text-xl font-semibold text-slate-900">Параметры запроса</h2>
              <p className="text-sm text-slate-500">Авторизация, сценарий и временной диапазон для выгрузки звонков.</p>
            </div>
            <button
              type="submit"
              disabled={processing}
              className="rounded-2xl bg-sky-600 px-5 py-3 text-sm font-semibold text-white transition hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-400"
            >
              {processing ? "Загрузка..." : "Загрузить звонки"}
            </button>
          </div>

          <div className="mt-6 grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
            <div className="space-y-4 rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-sky-100 text-sm font-semibold text-sky-700">1</div>
                <div>
                  <h3 className="font-semibold text-slate-800">Авторизация</h3>
                  <p className="text-sm text-slate-500">Подключение к Voximplant Kit.</p>
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-3">
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">Host</span>
                  <input
                    required
                    value={filters.api_host}
                    onChange={(e) => saveAuth({ api_host: e.target.value })}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                    placeholder="kitapi-ru.voximplant.com"
                  />
                </label>
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">Domain</span>
                  <input
                    required
                    value={filters.domain}
                    onChange={(e) => saveAuth({ domain: e.target.value })}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                    placeholder="ruse"
                  />
                </label>
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">access_token</span>
                  <input
                    required
                    type="password"
                    value={filters.access_token}
                    onChange={(e) => saveAuth({ access_token: e.target.value })}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                    placeholder="••••••••••"
                  />
                </label>
              </div>
            </div>

            <div className="space-y-4 rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-emerald-100 text-sm font-semibold text-emerald-700">2</div>
                <div>
                  <h3 className="font-semibold text-slate-800">Сценарий и фильтры</h3>
                  <p className="text-sm text-slate-500">Выберите сценарий и задайте временной диапазон.</p>
                </div>
              </div>

              <label className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium text-slate-700">Сценарий</span>
                  <button
                    type="button"
                    onClick={handleFetchScenarios}
                    disabled={loadingScenarios}
                    className="rounded-full bg-slate-200 px-3 py-1 text-xs font-semibold text-slate-700 transition hover:bg-slate-300 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {loadingScenarios ? "Загрузка..." : "Список сценариев"}
                  </button>
                </div>
                <input
                  value={filters.scenario_name}
                  onChange={(e) => saveAuth({ scenario_name: e.target.value, scenario_id: null })}
                  className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                  placeholder="Название сценария"
                />
                {filters.scenario_id && (
                  <p className="text-xs text-slate-500">Выбран ID сценария: {filters.scenario_id}</p>
                )}
                {isScenarioListOpen && scenarios.length > 0 && (
                  <div className="mt-2 space-y-2 rounded-2xl border border-slate-200 bg-white p-3">
                    <input
                      type="search"
                      value={scenarioFilter}
                      onChange={(e) => setScenarioFilter(e.target.value)}
                      className="w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm focus:border-sky-400 focus:outline-none"
                      placeholder="Быстрый поиск по сценариям"
                    />
                    <div className="max-h-40 overflow-y-auto rounded-2xl border border-slate-200 bg-slate-50 p-2">
                      {filteredScenarios.map((scenario) => (
                        <button
                          key={scenario.id}
                          type="button"
                          onClick={() => handleScenarioSelect(scenario)}
                          className="mb-2 w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-left text-sm text-slate-700 transition hover:bg-slate-100"
                        >
                          <div className="font-medium">{scenario.title}</div>
                          <div className="text-xs text-slate-500">ID: {scenario.id}</div>
                        </button>
                      ))}
                      {filteredScenarios.length === 0 && (
                        <div className="text-sm text-slate-500">Ничего не найдено</div>
                      )}
                    </div>
                  </div>
                )}
              </label>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">Дата/время с</span>
                  <input
                    type="datetime-local"
                    value={filters.from_date}
                    onChange={(e) => setFilters({ ...filters, from_date: e.target.value })}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                  />
                </label>
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">Дата/время по</span>
                  <input
                    type="datetime-local"
                    value={filters.to_date}
                    onChange={(e) => setFilters({ ...filters, to_date: e.target.value })}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                  />
                </label>
              </div>

              <div className="grid gap-4 md:grid-cols-[1fr_auto]">
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">Мин. длительность (сек)</span>
                  <input
                    type="number"
                    min="0"
                    value={filters.min_duration}
                    onChange={(e) => setFilters({ ...filters, min_duration: Number(e.target.value) })}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                  />
                </label>

                <label className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700">
                  <input
                    id="has_recording"
                    type="checkbox"
                    checked={filters.has_recording}
                    onChange={(e) => setFilters({ ...filters, has_recording: e.target.checked })}
                    className="h-4 w-4 rounded border-slate-300 text-sky-500 focus:ring-sky-400"
                  />
                  Только с записью
                </label>
              </div>

              <label className="space-y-2">
                <span className="text-sm font-medium text-slate-700">Максимум записей на странице</span>
                <select
                  value={recordsLimit}
                  onChange={(e) => setRecordsLimit(Number(e.target.value))}
                  className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-sky-400 focus:outline-none"
                >
                  <option value={50}>50 записей</option>
                  <option value={100}>100 записей</option>
                  <option value={200}>200 записей</option>
                  <option value={500}>500 записей</option>
                </select>
                <p className="text-xs text-slate-500">После достижения лимита появится кнопка "Загрузить ещё"</p>
              </label>
            </div>
          </div>

          <div className="mt-6 rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
            <div className="flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-violet-100 text-sm font-semibold text-violet-700">3</div>
              <div>
                <h3 className="font-semibold text-slate-800">Whisper</h3>
                <p className="text-sm text-slate-500">Выберите модель для последующей транскрибации.</p>
              </div>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {whisperModels.length > 0 ? (
                whisperModels.map((model) => {
                  const isSelected = selectedWhisperModel === model.name;
                  const isReady = model.status === "ready";
                  const isLoading = model.status === "loading";
                  const isError = model.status === "error";
                  const isBusy = downloadingModel === model.name || isLoading;
                  const statusToneClass = isReady
                    ? "text-emerald-700"
                    : isLoading
                      ? "text-amber-700"
                      : isError
                        ? "text-rose-700"
                        : "text-slate-500";

                  return (
                    <button
                      key={model.name}
                      type="button"
                      onClick={() => handleModelClick(model)}
                      disabled={isBusy}
                      className={`group w-full rounded-2xl border px-3 py-3 text-left text-sm transition ${
                        isSelected
                          ? "border-sky-500 bg-sky-50"
                          : isReady
                            ? "border-emerald-300 bg-emerald-50"
                            : isLoading
                              ? "border-amber-300 bg-amber-50"
                              : isError
                                ? "border-rose-300 bg-rose-50"
                                : "border-slate-200 bg-white"
                      } disabled:cursor-not-allowed disabled:opacity-70`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">{model.name}</span>
                            {isSelected && <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-sky-700">выбрана</span>}
                            {isReady && <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700">скачана</span>}
                          </div>
                          <div className={`mt-1 text-xs font-medium ${statusToneClass}`}>{model.status_label}</div>
                          {isReady && model.cache_path && (
                            <div className="mt-1 truncate text-[11px] text-emerald-700/90">Кэш: {model.cache_path}</div>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <span className={`h-2.5 w-2.5 rounded-full ${isReady ? "bg-emerald-500" : isLoading ? "bg-amber-500" : isError ? "bg-rose-500" : "bg-slate-300"}`} />
                          {!isReady && !isLoading && (
                            <span className="text-slate-400 opacity-0 transition group-hover:opacity-100">⬇</span>
                          )}
                        </div>
                      </div>
                      {isLoading && (
                        <div className="mt-2 h-2 rounded-full bg-white/80">
                          <div className="h-2 rounded-full bg-amber-500" style={{ width: `${model.progress || 0}%` }} />
                        </div>
                      )}
                    </button>
                  );
                })
              ) : (
                <div className="rounded-2xl border border-slate-200 bg-white px-3 py-3 text-sm text-slate-500">base</div>
              )}
            </div>
          </div>

          <div className="mt-6 rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-indigo-100 text-sm font-semibold text-indigo-700">3A</div>
                <div>
                  <h3 className="font-semibold text-slate-800">Диагностика производительности</h3>
                  <p className="text-sm text-slate-500">Текущее состояние параллелизма транскрибации и настроек CPU на бэкенде.</p>
                </div>
              </div>
              <label className="min-w-[260px] space-y-1">
                <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Профиль CPU</span>
                <select
                  value={selectedPerformanceProfile}
                  disabled={updatingPerformanceProfile}
                  onChange={(e) => void handlePerformanceProfileChange(e.target.value)}
                  className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 focus:border-indigo-400 focus:outline-none disabled:cursor-not-allowed disabled:bg-slate-100"
                >
                  <option value="max">Максимальная производительность (100% CPU)</option>
                  <option value="moderate">Умеренная (оставить 25% системе)</option>
                </select>
              </label>
            </div>

            {performanceDiagnostics ? (
              <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                  <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Ядра CPU (physical/logical)</div>
                  <div className="mt-1 text-2xl font-semibold text-slate-900">
                    {performanceDiagnostics.cpu?.physical_cores ?? "—"}
                    <span className="ml-1 text-sm font-medium text-slate-500">/ {performanceDiagnostics.cpu?.logical_cores ?? "—"}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    Доступно профилю: {performanceDiagnostics.cpu?.usable_cores ?? "—"}, резерв системе: {performanceDiagnostics.cpu?.reserved_cores ?? "—"}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                  <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Активные транскрибации</div>
                  <div className="mt-1 text-2xl font-semibold text-slate-900">
                    {performanceDiagnostics.transcription?.active ?? 0}
                    <span className="ml-1 text-sm font-medium text-slate-500">/ {performanceDiagnostics.transcription?.max_concurrency ?? "—"}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    Inference сейчас: {performanceDiagnostics.transcription?.active_inference ?? 0}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                  <div className="text-xs font-medium uppercase tracking-wide text-slate-500">PyTorch threads</div>
                  <div className="mt-1 text-2xl font-semibold text-slate-900">{performanceDiagnostics.torch?.num_threads ?? "—"}</div>
                  <div className="mt-1 text-xs text-slate-500">Interop: {performanceDiagnostics.torch?.interop_threads ?? "—"}</div>
                  <div className="mt-1 text-xs text-slate-500">
                    {performanceDiagnostics.torch?.pending_update ? "Изменения ожидают завершения текущих задач" : "Параметры применены"}
                  </div>
                </div>
                <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                  <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Слотов свободно</div>
                  <div className="mt-1 text-2xl font-semibold text-slate-900">{performanceDiagnostics.transcription?.available_slots ?? "—"}</div>
                  <div className="mt-1 text-xs text-slate-500">
                    {performanceDiagnostics.torch?.configured ? "PyTorch настроен" : "PyTorch будет настроен при первой транскрибации"}
                  </div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    Режим: {performanceDiagnostics.transcription?.model_inference_mode ?? "—"}, workers: {performanceDiagnostics.transcription?.queue_workers ?? "—"}
                  </div>
                </div>
              </div>
            ) : (
              <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-white px-4 py-3 text-sm text-slate-500">
                Подключение к диагностике производительности...
              </div>
            )}
          </div>

          <div className="mt-6 rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
            <div className="flex items-center gap-2">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-amber-100 text-sm font-semibold text-amber-700">4</div>
              <div>
                <h3 className="font-semibold text-slate-800">Фоновое расписание</h3>
                <p className="text-sm text-slate-500">Сервер будет автоматически загружать звонки и транскрибировать их даже при закрытой веб-странице.</p>
              </div>
            </div>

            <div className="mt-4 grid gap-4 md:grid-cols-2">
              <label className="space-y-2">
                <span className="text-sm font-medium text-slate-700">Название расписания</span>
                <input
                  value={scheduleForm.name}
                  onChange={(e) => setScheduleForm((prev) => ({ ...prev, name: e.target.value }))}
                  className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-amber-400 focus:outline-none"
                  placeholder="Будни рабочее время"
                />
              </label>
              <div className="grid gap-4 grid-cols-2">
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">Час с</span>
                  <select
                    value={scheduleForm.from_hour}
                    onChange={(e) => setScheduleForm((prev) => ({ ...prev, from_hour: Number(e.target.value) }))}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-amber-400 focus:outline-none"
                  >
                    {Array.from({ length: 24 }, (_, hour) => (
                      <option key={`from-${hour}`} value={hour}>{hour.toString().padStart(2, "0")}:00</option>
                    ))}
                  </select>
                </label>
                <label className="space-y-2">
                  <span className="text-sm font-medium text-slate-700">Час по</span>
                  <select
                    value={scheduleForm.to_hour}
                    onChange={(e) => setScheduleForm((prev) => ({ ...prev, to_hour: Number(e.target.value) }))}
                    className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-amber-400 focus:outline-none"
                  >
                    {Array.from({ length: 24 }, (_, hour) => (
                      <option key={`to-${hour}`} value={hour}>{hour.toString().padStart(2, "0")}:00</option>
                    ))}
                  </select>
                </label>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              {WEEKDAY_OPTIONS.map((day) => {
                const selected = scheduleForm.weekdays.includes(day.value);
                return (
                  <button
                    key={day.value}
                    type="button"
                    onClick={() => toggleWeekday(day.value)}
                    className={`rounded-full px-3 py-1 text-sm font-semibold transition ${selected ? "bg-amber-600 text-white" : "bg-white border border-slate-200 text-slate-700 hover:bg-slate-100"}`}
                  >
                    {day.label}
                  </button>
                );
              })}
            </div>

            <div className="mt-4 grid gap-4 md:grid-cols-[1fr_auto]">
              <label className="space-y-2">
                <span className="text-sm font-medium text-slate-700">Мин. длительность для расписания (сек)</span>
                <input
                  type="number"
                  min="0"
                  value={scheduleForm.min_duration}
                  onChange={(e) => setScheduleForm((prev) => ({ ...prev, min_duration: Number(e.target.value) }))}
                  className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm focus:border-amber-400 focus:outline-none"
                />
              </label>
              <label className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700">
                <input
                  id="schedule_has_recording"
                  type="checkbox"
                  checked={scheduleForm.has_recording}
                  onChange={(e) => setScheduleForm((prev) => ({ ...prev, has_recording: e.target.checked }))}
                  className="h-4 w-4 rounded border-slate-300 text-amber-500 focus:ring-amber-400"
                />
                Только звонки с записью
              </label>
            </div>

            <div className="mt-4 flex flex-wrap gap-3">
              <button
                type="button"
                onClick={handleCreateSchedule}
                disabled={creatingSchedule}
                className="rounded-full bg-amber-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:bg-slate-400"
              >
                {creatingSchedule ? "Создание..." : "Создать расписание"}
              </button>
              <button
                type="button"
                onClick={refreshSchedules}
                disabled={loadingSchedules}
                className="rounded-full border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-700 transition hover:bg-slate-100 disabled:opacity-60"
              >
                {loadingSchedules ? "Обновление..." : "Обновить список"}
              </button>
            </div>

            <div className="mt-4 space-y-2">
              {schedules.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-slate-200 bg-white px-3 py-3 text-sm text-slate-500">
                  Расписания пока не созданы.
                </div>
              ) : (
                schedules.map((schedule) => (
                  <div key={schedule.id} className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <div className="font-semibold text-slate-800">{schedule.name}</div>
                        <div className="text-xs text-slate-500">
                          Дни: {(schedule.weekdays || []).map((d) => WEEKDAY_OPTIONS.find((w) => w.value === d)?.label || d).join(", ")} | Часы: {String(schedule.from_hour).padStart(2, "0")}:00-{String(schedule.to_hour).padStart(2, "0")}:59
                        </div>
                        <div className="text-xs text-slate-500 mt-1">
                          Фильтры: мин. длительность {schedule.min_duration || 0} сек, {schedule.has_recording ? "только с записью" : "все звонки"}
                        </div>
                        {schedule.last_result && (
                          <div className="text-xs text-slate-500 mt-1">
                            Последний запуск: найдено {schedule.last_result.calls_found}, транскрибировано {schedule.last_result.transcribed}, из кэша {schedule.last_result.cached}, ошибок {schedule.last_result.failed}
                          </div>
                        )}
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          onClick={() => handleToggleSchedule(schedule)}
                          className={`rounded-full px-3 py-1 text-xs font-semibold transition ${schedule.enabled ? "bg-emerald-100 text-emerald-800" : "bg-slate-200 text-slate-700"}`}
                        >
                          {schedule.enabled ? "Включено" : "Отключено"}
                        </button>
                        <button
                          type="button"
                          onClick={() => handleRunScheduleNow(schedule.id)}
                          className="rounded-full border border-slate-300 bg-white px-3 py-1 text-xs font-semibold text-slate-700 transition hover:bg-slate-100"
                        >
                          Запустить сейчас
                        </button>
                        <button
                          type="button"
                          onClick={() => handleDeleteSchedule(schedule.id)}
                          className="rounded-full border border-rose-300 bg-rose-50 px-3 py-1 text-xs font-semibold text-rose-700 transition hover:bg-rose-100"
                        >
                          Удалить
                        </button>
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </form>

        <section className="rounded-3xl bg-white p-6 shadow-xl shadow-slate-200/80">
          <h2 className="text-xl font-semibold">Статус работы</h2>
          <div className="mt-4 max-h-48 space-y-2 overflow-y-auto rounded-xl border border-slate-200 bg-slate-50 p-4 text-slate-700">
            {statusMessages.length === 0 ? (
              <p className="text-slate-500">Здесь будут сообщения в реальном времени...</p>
            ) : (
              statusMessages.map((line, index) => (
                <div key={index} className="text-sm leading-6">
                  {line}
                </div>
              ))
            )}
          </div>
        </section>

        <section className="rounded-3xl bg-white p-6 shadow-xl shadow-slate-200/80">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-xl font-semibold">Список звонков</h2>
              <p className="text-sm text-slate-500">Компактный вид для быстрого анализа и сравнения.</p>
              <div className="mt-3 inline-flex rounded-xl border border-slate-200 bg-slate-100 p-1">
                <button
                  type="button"
                  onClick={() => setActiveCallsTab("all")}
                  className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition ${activeCallsTab === "all" ? "bg-white text-slate-900 shadow-sm" : "text-slate-600 hover:text-slate-800"}`}
                >
                  Все звонки ({records.length})
                </button>
                <button
                  type="button"
                  onClick={() => setActiveCallsTab("transcribed")}
                  className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition ${activeCallsTab === "transcribed" ? "bg-white text-slate-900 shadow-sm" : "text-slate-600 hover:text-slate-800"}`}
                >
                  Все транскрибации ({transcribedRecords.length})
                </button>
                <button
                  type="button"
                  onClick={() => setActiveCallsTab("queue")}
                  className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition ${activeCallsTab === "queue" ? "bg-white text-slate-900 shadow-sm" : "text-slate-600 hover:text-slate-800"}`}
                >
                  Очередь ({queueCounters.total || 0})
                </button>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {activeCallsTab === "transcribed" && (
                <>
                  <label className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-sm text-slate-700">
                    <span className="font-medium text-slate-600">Сценарий:</span>
                    <select
                      value={transcribedScenarioFilter}
                      onChange={(e) => setTranscribedScenarioFilter(e.target.value)}
                      className="bg-transparent text-sm font-medium text-slate-700 focus:outline-none"
                    >
                      <option value="all">Все сценарии</option>
                      {availableTranscribedScenarios.map((scenarioName) => (
                        <option key={scenarioName} value={scenarioName}>{scenarioName}</option>
                      ))}
                    </select>
                  </label>

                  <label className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-sm text-slate-700">
                    <span className="font-medium text-slate-600">Период:</span>
                    <select
                      value={transcribedDatePreset}
                      onChange={(e) => setTranscribedDatePreset(e.target.value)}
                      className="bg-transparent text-sm font-medium text-slate-700 focus:outline-none"
                    >
                      <option value="all">За все время</option>
                      <option value="today">Сегодня</option>
                      <option value="24h">Последние 24 часа</option>
                      <option value="7d">Последние 7 дней</option>
                      <option value="30d">Последние 30 дней</option>
                      <option value="custom">Свой диапазон</option>
                    </select>
                  </label>

                  <label className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-sm text-slate-700">
                    <span className="font-medium text-slate-600">С:</span>
                    <input
                      type="datetime-local"
                      value={transcribedDateFrom}
                      max={transcribedDateTo || undefined}
                      onChange={(e) => {
                        setTranscribedDatePreset("custom");
                        setTranscribedDateFrom(e.target.value);
                      }}
                      className="bg-transparent text-sm font-medium text-slate-700 focus:outline-none"
                    />
                  </label>

                  <label className="flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-sm text-slate-700">
                    <span className="font-medium text-slate-600">По:</span>
                    <input
                      type="datetime-local"
                      value={transcribedDateTo}
                      min={transcribedDateFrom || undefined}
                      onChange={(e) => {
                        setTranscribedDatePreset("custom");
                        setTranscribedDateTo(e.target.value);
                      }}
                      className="bg-transparent text-sm font-medium text-slate-700 focus:outline-none"
                    />
                  </label>

                  <button
                    type="button"
                    onClick={() => {
                      setTranscribedDatePreset("all");
                      setTranscribedDateFrom("");
                      setTranscribedDateTo("");
                    }}
                    className="rounded-full border border-slate-300 bg-white px-3 py-1 text-sm font-semibold text-slate-700 transition hover:bg-slate-100"
                  >
                    Сбросить период
                  </button>
                </>
              )}
              <div className="rounded-full bg-slate-100 px-3 py-1 text-sm font-medium text-slate-700">
                {visibleRecords.length} записей
              </div>
              {activeCallsTab !== "queue" && (
                <button
                  type="button"
                  onClick={handleAddVisibleToQueue}
                  disabled={visibleRecords.filter((record) => record.record_url).length === 0 || Boolean(queueBusyAction)}
                  className="rounded-full border border-indigo-300 bg-indigo-50 px-3 py-1 text-sm font-semibold text-indigo-700 transition hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Добавить видимые в очередь
                </button>
              )}
              <button
                type="button"
                onClick={handleDownloadTranscriptsXlsx}
                disabled={transcribedRecords.length === 0 || downloadingExport}
                className="rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1 text-sm font-semibold text-emerald-700 transition hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {downloadingExport ? "Подготовка..." : "Скачать XLSX"}
              </button>
              {activeCallsTab === "queue" && (
                <>
                  <button
                    type="button"
                    onClick={handleStartQueue}
                    disabled={Boolean(queueBusyAction) || queueCounters.queued === 0 || queueRunner.running}
                    className="rounded-full bg-violet-600 px-3 py-1 text-sm font-semibold text-white transition hover:bg-violet-700 disabled:cursor-not-allowed disabled:bg-slate-400"
                  >
                    Транскрибировать всю очередь
                  </button>
                  <button
                    type="button"
                    onClick={handleStopQueue}
                    disabled={Boolean(queueBusyAction) || !queueRunner.running}
                    className="rounded-full border border-amber-300 bg-amber-50 px-3 py-1 text-sm font-semibold text-amber-700 transition hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    Остановить
                  </button>
                  <button
                    type="button"
                    onClick={handleRetryFailedQueue}
                    disabled={Boolean(queueBusyAction) || queueCounters.failed === 0}
                    className="rounded-full border border-rose-300 bg-rose-50 px-3 py-1 text-sm font-semibold text-rose-700 transition hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    Повторить failed
                  </button>
                  <button
                    type="button"
                    onClick={handleRetrySkippedQueue}
                    disabled={Boolean(queueBusyAction) || queueCounters.skipped === 0}
                    className="rounded-full border border-orange-300 bg-orange-50 px-3 py-1 text-sm font-semibold text-orange-700 transition hover:bg-orange-100 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    Повторить skipped
                  </button>
                  <button
                    type="button"
                    onClick={() => handleClearQueueByStatuses(["done", "failed", "skipped"]) }
                    disabled={Boolean(queueBusyAction) || (queueCounters.done + queueCounters.failed + queueCounters.skipped === 0)}
                    className="rounded-full border border-slate-300 bg-white px-3 py-1 text-sm font-semibold text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    Очистить завершенные
                  </button>
                </>
              )}
              <button
                type="button"
                onClick={() => setShowFavoritesOnly((prev) => !prev)}
                className={`rounded-full px-3 py-1 text-sm font-semibold transition ${showFavoritesOnly ? "bg-amber-600 text-white" : "bg-slate-200 text-slate-700 hover:bg-slate-300"}`}
              >
                {showFavoritesOnly ? "Показать все" : "Только избранные"}
              </button>
              {activeCallsTab !== "queue" && bulkTranscribingInProgress ? (
                <div className="flex items-center gap-3">
                  <div className="text-sm font-medium text-amber-700">
                    Транскрибирование {bulkTranscribingProgress.current} из {bulkTranscribingProgress.total}
                  </div>
                  <button
                    type="button"
                    onClick={handleCancelBulkTranscribe}
                    className="rounded-full border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-semibold text-amber-700 transition hover:bg-amber-100"
                  >
                    Отмена
                  </button>
                </div>
              ) : activeCallsTab !== "queue" ? (
                <button
                  type="button"
                  onClick={handleBulkTranscribe}
                  disabled={records.length === 0 || transcribingIds.length > 0}
                  className="rounded-full bg-violet-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-violet-700 disabled:cursor-not-allowed disabled:bg-slate-400"
                >
                  Транскрибировать все
                </button>
              ) : null}
            </div>
          </div>

          {bulkTranscribingInProgress && (
            <div className="mt-4 rounded-2xl bg-amber-50 border border-amber-200 p-4">
              <div className="flex items-center justify-between gap-3 mb-2">
                <span className="text-sm font-medium text-amber-700">Ход массовой транскрибации</span>
                <span className="text-sm text-amber-600">{Math.round((bulkTranscribingProgress.current / bulkTranscribingProgress.total) * 100)}%</span>
              </div>
              <div className="h-2 rounded-full bg-white/80 overflow-hidden">
                <div
                  className="h-full bg-amber-500 transition-all duration-300"
                  style={{ width: `${(bulkTranscribingProgress.current / bulkTranscribingProgress.total) * 100}%` }}
                />
              </div>
            </div>
          )}

          {recordsByTab.length === 0 ? (
            <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-8 text-center text-sm text-slate-500">
              {activeCallsTab === "transcribed"
                ? "Здесь будут все сохранённые транскрипции."
                : activeCallsTab === "queue"
                  ? "Очередь пока пуста. Добавьте звонки из списка во вкладке 'Все звонки'."
                  : "Здесь появятся звонки после загрузки истории."}
            </div>
          ) : visibleRecords.length === 0 ? (
            <div className="mt-4 rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-8 text-center text-sm text-slate-500">
              {activeCallsTab === "transcribed"
                ? "Нет транскрибированных звонков. Запустите транскрибацию хотя бы одной записи."
                : activeCallsTab === "queue"
                  ? "Очередь не содержит записей по текущим условиям отображения."
                  : "Нет звонков для отображения. Снимите фильтр \"Только избранные\" или добавьте записи в избранное."}
            </div>
          ) : activeCallsTab === "queue" ? (
            <div className="mt-4 space-y-4">
              <div className="grid gap-3 md:grid-cols-4 xl:grid-cols-7">
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">Всего: <span className="font-semibold">{queueCounters.total || 0}</span></div>
                <div className="rounded-2xl border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm text-indigo-700">queued: <span className="font-semibold">{queueCounters.queued || 0}</span></div>
                <div className="rounded-2xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">processing: <span className="font-semibold">{queueCounters.processing || 0}</span></div>
                <div className="rounded-2xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">done: <span className="font-semibold">{queueCounters.done || 0}</span></div>
                <div className="rounded-2xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">failed: <span className="font-semibold">{queueCounters.failed || 0}</span></div>
                <div className="rounded-2xl border border-orange-200 bg-orange-50 px-3 py-2 text-sm text-orange-700">skipped: <span className="font-semibold">{queueCounters.skipped || 0}</span></div>
                <div className="rounded-2xl border border-slate-200 bg-slate-100 px-3 py-2 text-sm text-slate-700">canceled: <span className="font-semibold">{queueCounters.canceled || 0}</span></div>
              </div>

              <div className="text-xs text-slate-500">
                {loadingQueue ? "Загрузка очереди..." : queueRunner.running ? "Очередь выполняется" : "Очередь в ожидании"}
                {queueRunner.stop_requested ? ", остановка запрошена" : ""}
              </div>

              <div className="overflow-x-auto">
                <table className="min-w-full border-separate border-spacing-0 text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-left text-slate-600">
                      <th className="rounded-tl-2xl px-3 py-3 font-medium">#</th>
                      <th className="px-3 py-3 font-medium">ID</th>
                      <th className="px-3 py-3 font-medium">Дата</th>
                      <th className="px-3 py-3 font-medium">caller_a</th>
                      <th className="px-3 py-3 font-medium">caller_b</th>
                      <th className="px-3 py-3 font-medium">Статус</th>
                      <th className="px-3 py-3 font-medium">Ошибка</th>
                      <th className="rounded-tr-2xl px-3 py-3 text-right font-medium">Действия</th>
                    </tr>
                  </thead>
                  <tbody>
                    {queueItems.map((item, index) => {
                      const statusTone = item.status === "done"
                        ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                        : item.status === "failed"
                          ? "bg-rose-50 text-rose-700 border-rose-200"
                          : item.status === "skipped"
                            ? "bg-orange-50 text-orange-700 border-orange-200"
                          : item.status === "processing"
                            ? "bg-amber-50 text-amber-700 border-amber-200"
                            : item.status === "canceled"
                              ? "bg-slate-100 text-slate-700 border-slate-200"
                              : "bg-indigo-50 text-indigo-700 border-indigo-200";

                      return (
                        <tr key={`queue-${item.call_id}`} className="border-b border-slate-100 align-top hover:bg-slate-50">
                          <td className="px-3 py-3 font-semibold text-slate-500">{index + 1}</td>
                          <td className="px-3 py-3 text-slate-700">{item.call_id}</td>
                          <td className="px-3 py-3 text-slate-700">{item.datetime_start ? formatCallDateTime({ datetime_start: item.datetime_start, timezone: DEFAULT_CALL_TIMEZONE }) : "—"}</td>
                          <td className="px-3 py-3 text-slate-700">{item.caller_a || "—"}</td>
                          <td className="px-3 py-3 text-slate-700">{item.caller_b || "—"}</td>
                          <td className="px-3 py-3">
                            <span className={`inline-flex rounded-full border px-2 py-1 text-xs font-semibold ${statusTone}`}>{item.status}</span>
                          </td>
                          <td className="px-3 py-3 text-xs text-rose-700 max-w-[280px] whitespace-pre-wrap break-words">{item.error_message || "—"}</td>
                          <td className="px-3 py-3 text-right">
                            <button
                              type="button"
                              onClick={() => handleDeleteQueueItem(item.call_id)}
                              disabled={item.status === "processing" || Boolean(queueBusyAction)}
                              className="rounded-full border border-slate-300 bg-white px-3 py-1 text-xs font-semibold text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
                            >
                              Удалить
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <>
              <div className="mt-4 overflow-x-auto">
                <table className="min-w-full border-separate border-spacing-0 text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-left text-slate-600">
                      <th className="rounded-tl-2xl px-3 py-3 font-medium">#</th>
                      <th className="px-3 py-3 font-medium">Дата</th>
                      <th className="px-3 py-3 font-medium">caller_a</th>
                      <th className="px-3 py-3 font-medium">caller_b</th>
                      <th className="px-3 py-3 font-medium text-right">
                        <button
                          type="button"
                          onClick={toggleDurationSort}
                          className="inline-flex items-center gap-1 rounded-full px-2 py-1 font-medium text-slate-600 transition hover:bg-slate-200 hover:text-slate-900"
                          title="Сортировать по длительности"
                        >
                          Длительность <span aria-hidden="true">{durationSortLabel}</span>
                        </button>
                      </th>
                      <th className="px-3 py-3 font-medium">Транскрибация</th>
                      <th className="rounded-tr-2xl px-3 py-3 text-right font-medium">Действия</th>
                    </tr>
                  </thead>
                <tbody>
                  {visibleRecords.map((record, index) => {
                    const callerA = record.caller_a || record.phone_a || "—";
                    const callerB = record.caller_b || record.phone_b || "—";
                    const transcriptText = record.transcript?.trim();
                    const isTranscribing = transcribingIds.includes(record.id);
                    const isPlaying = playingAudioId === record.id;
                    const isAudioPanelOpen = activeAudioPanelId === record.id;
                    const isAudioPreparing = preparingAudioIds.includes(record.id);
                    const isDownloadingAudio = downloadingAudioIds.includes(record.id);
                    const isFavorite = favoriteIds.has(record.id);
                    const isCommentEditorOpen = openFavoriteCommentId === record.id;
                    const favoriteComment = (favoriteComments[record.id] || "").trim();
                    const audioProgress = audioProgressById[record.id] || { currentTime: 0, duration: 0 };
                    const timelineMax = audioProgress.duration > 0 ? audioProgress.duration : 0;
                    const timelineValue = Math.min(audioProgress.currentTime, timelineMax || 0);
                    const queueItem = queueByCallId.get(normalizeRecordId(record.id));
                    const queueStatus = queueItem?.status;
                    const isAlreadyInQueue = Boolean(queueItem);

                    return (
                      <Fragment key={record.id}>
                        <tr className="border-b border-slate-100 align-top hover:bg-slate-50">
                          <td className="px-3 py-3 font-semibold text-slate-500">{index + 1}</td>
                          <td className="px-3 py-3">
                            <div className="font-medium text-slate-800">{formatCallDateTime(record)}</div>
                            <div className="mt-1 text-xs text-slate-500">{getCallTimezone(record)}</div>
                          </td>
                          <td className="px-3 py-3 text-slate-700">{callerA}</td>
                          <td className="px-3 py-3 text-slate-700">{callerB}</td>
                          <td className="px-3 py-3 text-right font-medium text-slate-700">
                            {Number.isFinite(Number(record.duration)) ? `${Number(record.duration)} сек` : "—"}
                          </td>
                          <td className="min-w-[320px] px-3 py-3">
                            {transcriptText ? (
                              <div className="max-h-24 overflow-y-auto whitespace-pre-wrap text-sm leading-6 text-slate-700">
                                {transcriptText}
                              </div>
                            ) : (
                              <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500">
                                {record.record_url ? "Текст ещё не готов. Нажмите для транскрибации." : "Запись отсутствует"}
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-3">
                            {showFavoritesOnly && favoriteComment && (
                              <div className="mb-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                                <span className="font-semibold">Комментарий:</span> {favoriteComment}
                              </div>
                            )}
                            <div className="flex flex-wrap justify-end gap-2">
                              <button
                                type="button"
                                onClick={() => toggleFavorite(record.id)}
                                className={`rounded-full px-2 py-2 text-xs font-semibold transition ${isFavorite ? "bg-amber-100 text-amber-900" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}
                              >
                                {isFavorite ? "★" : "☆"}
                              </button>
                              <button
                                type="button"
                                onClick={() => handleAddToQueue(record)}
                                disabled={!record.record_url || isAlreadyInQueue}
                                className="rounded-full border border-indigo-300 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 transition hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-60"
                              >
                                {isAlreadyInQueue ? `В очереди (${queueStatus})` : "Добавить в очередь"}
                              </button>
                              <button
                                type="button"
                                onClick={() => handleTranscribe(record)}
                                disabled={!record.record_url || isTranscribing}
                                className="rounded-full bg-sky-600 px-3 py-2 text-xs font-semibold text-white transition hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-400"
                              >
                                {isTranscribing ? "Транскрибируем..." : "Транскрибировать"}
                              </button>
                              <button
                                type="button"
                                onClick={() => toggleAudioPlayback(record)}
                                disabled={!record.record_url || isAudioPreparing}
                                className="rounded-full border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
                              >
                                {isAudioPreparing ? "Загрузка..." : isPlaying ? "⏸ Pause" : "▶ Прослушать"}
                              </button>
                              <button
                                type="button"
                                onClick={() => handleDownloadAudio(record)}
                                disabled={(!record.record_url && !record.local_audio_url) || isDownloadingAudio}
                                className="rounded-full border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-700 transition hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-60"
                              >
                                {isDownloadingAudio ? "Скачивание..." : "⬇ Скачать"}
                              </button>
                              <audio
                                key={`audio-${record.id}`}
                                ref={(element) => {
                                  if (element) {
                                    audioRefs.current[record.id] = element;
                                  }
                                }}
                                src={record.local_audio_url || record.record_url || undefined}
                                preload="none"
                                crossOrigin="anonymous"
                                onTimeUpdate={(event) => handleAudioTimeUpdate(record.id, event)}
                                onLoadedMetadata={(event) => handleAudioMetadataLoaded(record.id, event)}
                                onEnded={() => handleAudioEnded(record.id)}
                                onError={(e) => console.error(`[AUDIO] Error loading audio for ${record.id}:`, e.target.error)}
                                className="hidden"
                              />
                            </div>
                            {isFavorite && (
                              <div className="mt-2 rounded-2xl border border-amber-200 bg-amber-50/70 p-3">
                                {isCommentEditorOpen ? (
                                  <>
                                    <label className="mb-2 block text-xs font-semibold text-amber-900">Комментарий к избранному звонку</label>
                                    <textarea
                                      value={favoriteCommentDrafts[record.id] || ""}
                                      onChange={(e) => handleFavoriteCommentDraftChange(record.id, e.target.value)}
                                      rows={3}
                                      placeholder="Добавьте комментарий"
                                      className="w-full rounded-xl border border-amber-200 bg-white px-3 py-2 text-xs text-slate-700 focus:border-amber-400 focus:outline-none"
                                    />
                                    <div className="mt-2 flex justify-end gap-2">
                                      <button
                                        type="button"
                                        onClick={() => setOpenFavoriteCommentId(null)}
                                        className="rounded-full border border-slate-300 bg-white px-3 py-1 text-xs font-semibold text-slate-700 transition hover:bg-slate-100"
                                      >
                                        Отмена
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => handleSaveFavoriteComment(record.id)}
                                        className="rounded-full bg-amber-600 px-3 py-1 text-xs font-semibold text-white transition hover:bg-amber-700"
                                      >
                                        Сохранить комментарий
                                      </button>
                                    </div>
                                  </>
                                ) : (
                                  <div className="flex items-center justify-between gap-2">
                                    <div className="min-w-0 text-xs text-amber-900">
                                      {favoriteComment ? (
                                        <span className="line-clamp-2"><span className="font-semibold">Комментарий:</span> {favoriteComment}</span>
                                      ) : (
                                        <span>Комментарий не добавлен.</span>
                                      )}
                                    </div>
                                    <button
                                      type="button"
                                      onClick={() => {
                                        setFavoriteCommentDrafts((prev) => ({
                                          ...prev,
                                          [record.id]: prev[record.id] ?? favoriteComment,
                                        }));
                                        setOpenFavoriteCommentId(record.id);
                                      }}
                                      className="rounded-full border border-amber-300 bg-white px-3 py-1 text-xs font-semibold text-amber-800 transition hover:bg-amber-100"
                                    >
                                      {favoriteComment ? "Изменить" : "Добавить"}
                                    </button>
                                  </div>
                                )}
                              </div>
                            )}
                          </td>
                        </tr>
                        {isAudioPanelOpen && (
                          <tr className="border-b border-slate-100 bg-slate-50/70">
                            <td colSpan={7} className="px-3 pb-4 pt-1">
                              <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
                                <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
                                  <span>{isAudioPreparing ? "Подготавливаем аудио..." : isPlaying ? "Сейчас воспроизводится" : "Пауза"}</span>
                                  <span>
                                    {formatAudioTimelineTime(timelineValue)} / {formatAudioTimelineTime(timelineMax)}
                                  </span>
                                </div>
                                <input
                                  type="range"
                                  min={0}
                                  max={timelineMax || 0}
                                  step={0.1}
                                  value={timelineValue}
                                  onChange={(event) => handleAudioSeek(record.id, event.target.value)}
                                  disabled={isAudioPreparing || timelineMax <= 0}
                                  className="h-2 w-full cursor-pointer appearance-none rounded-lg bg-slate-200 accent-sky-600 disabled:cursor-not-allowed disabled:opacity-60"
                                />
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
              </div>

              {activeCallsTab === "all" && canLoadMore && (
                <div className="mt-4 flex justify-center">
                  <button
                    type="button"
                    onClick={handleLoadMore}
                    disabled={loadingMore}
                    className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-slate-400"
                  >
                    {loadingMore ? "Загрузка..." : "Загрузить ещё записи"}
                  </button>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}

export default App;
