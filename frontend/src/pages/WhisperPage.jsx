import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import { settingsApi } from "../api/endpoints";
import { useAuth } from "../auth/AuthContext";
import { Badge, Button, Card, EmptyState, ErrorBanner } from "../components/ui";

/**
 * Whisper settings: hardware, model management, performance profile.
 *
 * Server-wide settings, so the whole page is ADMIN-only: a non-admin gets a
 * read-only notice instead, matching the backend's 403 on every endpoint here.
 *
 * The default model and the profile are persisted by the backend, never in the
 * browser: a reload re-reads GET /api/settings/current and shows the same state.
 */

const PROFILE_MAX = "max";
const PROFILE_MODERATE = "moderate";

// While a download is running the model list is the only progress signal the
// backend offers, so poll it instead of drawing a fake progress bar.
const DOWNLOAD_POLL_MS = 2500;

const PROFILE_CARDS = [
  {
    id: PROFILE_MODERATE,
    title: "Оптимальный",
    description:
      "Баланс скорости и нагрузки. Часть CPU/RAM остаётся для ОС и других сервисов.",
    planKey: "moderate_plan",
  },
  {
    id: PROFILE_MAX,
    title: "Максимальная производительность",
    description:
      "Использует почти все доступные ресурсы сервера для транскрибации.",
    planKey: "max_plan",
  },
];

const MODEL_STATUS_LABELS = {
  ready: { text: "Скачана", tone: "success" },
  loading: { text: "Скачивается...", tone: "warning" },
  downloading: { text: "Скачивается...", tone: "warning" },
  missing: { text: "Не скачана", tone: "neutral" },
  error: { text: "Ошибка", tone: "danger" },
};

const LIMIT_REASONS = {
  cpu: "Ограничено количеством доступных CPU-ядер.",
  ram: "Ограничено объёмом свободной RAM: выбранная модель требует больше памяти.",
  inference_isolation:
    "Инференс выполняется последовательно (whisper_inference_isolation=serialized): одновременно обрабатывается один файл.",
};

/** Bytes to a human GB/MB string. Hardware endpoint reports raw bytes. */
function formatBytes(value) {
  if (value === null || value === undefined) return "—";
  const gb = value / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.round(value / 1024 ** 2)} MB`;
}

function formatMb(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 1024) return `${(value / 1024).toFixed(1)} GB`;
  return `${value} MB`;
}

/** Turn an ApiError into something a human can act on. */
function describeError(error) {
  if (!error) return null;
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return {
        code: error.code,
        message: "Недостаточно прав: настройки Whisper доступны только администратору.",
      };
    }
    if (error.status === 0 || error.code === "HTTP_502" || error.code === "HTTP_504") {
      return { code: error.code, message: "Бэкенд недоступен. Повторите попытку позже." };
    }
    return { code: error.code, message: error.message };
  }
  // Network failure: fetch rejects before any HTTP status exists.
  return { code: null, message: "Не удалось связаться с сервером." };
}

function Row({ label, value, hint }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 py-1.5">
      <dt className="text-sm text-slate-500">{label}</dt>
      <dd className="text-sm font-medium text-slate-900">
        {value}
        {hint && <span className="ml-2 text-xs font-normal text-slate-400">{hint}</span>}
      </dd>
    </div>
  );
}

function HardwareSection({ hardware, loading, error }) {
  if (loading) {
    return (
      <Card title="Сервер">
        <EmptyState title="Загрузка..." />
      </Card>
    );
  }

  if (error) {
    const desc = describeError(error);
    return (
      <Card title="Сервер">
        <ErrorBanner error={desc} />
      </Card>
    );
  }

  if (!hardware) return null;

  const cpu = hardware.cpu || {};
  const mem = hardware.memory || {};
  const gpu = hardware.gpu || {};
  const torch = hardware.torch || {};
  const platform = hardware.platform || {};

  const gpuAvailable = gpu.available && gpu.devices && gpu.devices.length > 0;
  const gpuDevice = gpuAvailable ? gpu.devices[0] : null;

  return (
    <Card title="Сервер" description="Обнаруженные ресурсы сервера">
      <dl className="divide-y divide-slate-100">
        <Row
          label="CPU"
          value={`${cpu.physical || 1} ядер / ${cpu.logical || 1} потоков`}
        />
        <Row
          label="Доступно сервису"
          value={`${cpu.effective || 1} потоков`}
          hint={cpu.source || ""}
        />
        <Row
          label="RAM"
          value={`${formatBytes(mem.total_bytes)} / свободно ${formatBytes(mem.available_bytes)}`}
        />
        <Row label="Архитектура" value={platform.architecture || "—"} />

        {gpuAvailable ? (
          <>
            <Row label="GPU" value={gpuDevice?.name || "доступно"} />
            <Row label="Backend" value={(gpu.backend || "").toUpperCase()} />
            {gpuDevice?.vram_bytes && (
              <Row label="VRAM" value={formatBytes(gpuDevice.vram_bytes)} />
            )}
          </>
        ) : (
          <Row label="GPU" value="недоступно" hint={gpu.reason || ""} />
        )}

        {torch.installed ? (
          <>
            <Row label="Torch" value={torch.version || "установлен"} />
            <Row label="CUDA" value={torch.cuda_available ? "доступна" : "недоступна"} />
            <Row label="MPS" value={torch.mps_available ? "доступно" : "недоступно"} />
          </>
        ) : (
          <Row label="Torch" value="не установлен" />
        )}
      </dl>
    </Card>
  );
}

function ModelRow({ model, busy, onDownload, onUseDefault, onDelete }) {
  const status = MODEL_STATUS_LABELS[model.status] ?? MODEL_STATUS_LABELS.missing;
  const isDownloading = model.status === "loading" || model.status === "downloading";
  const isReady = model.status === "ready" || model.cached;

  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-slate-900">{model.name}</span>
          <Badge tone={status.tone}>{status.text}</Badge>
          {model.is_default && <Badge tone="success">Используется по умолчанию</Badge>}
        </div>
        <p className="mt-1 text-xs text-slate-500">
          Примерный размер: {formatMb(model.size_mb)}
        </p>
        {model.error && <p className="mt-1 text-xs text-red-600">{model.error}</p>}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {!isReady && (
          <Button variant="secondary" onClick={onDownload} disabled={busy || isDownloading}>
            {isDownloading ? "Скачивается..." : "Скачать"}
          </Button>
        )}
        <Button
          variant={model.is_default ? "primary" : "secondary"}
          onClick={onUseDefault}
          disabled={busy || model.is_default || isDownloading}
        >
          {model.is_default ? "Используется" : "Использовать"}
        </Button>
        <Button
          variant="danger"
          onClick={onDelete}
          disabled={busy || !isReady || isDownloading || model.is_default}
          // The backend also refuses a loaded/downloading model; disabling the
          // default here avoids an obvious round-trip to a 409.
          title={model.is_default ? "Нельзя удалить модель по умолчанию" : undefined}
        >
          Удалить
        </Button>
      </div>
    </li>
  );
}

function ModelsSection({ models, loading, error, busyModel, onDownload, onUseDefault, onDelete }) {
  if (loading) {
    return (
      <Card title="Модель Whisper">
        <EmptyState title="Загрузка..." />
      </Card>
    );
  }

  if (error) {
    return (
      <Card title="Модель Whisper">
        <ErrorBanner error={describeError(error)} />
      </Card>
    );
  }

  if (!models || models.length === 0) {
    return (
      <Card title="Модель Whisper">
        <EmptyState
          title="Нет доступных моделей"
          description="Бэкенд не вернул список поддерживаемых моделей."
        />
      </Card>
    );
  }

  return (
    <Card
      title="Модель Whisper"
      description="Модель фиксируется в момент добавления в очередь: уже добавленные задачи не меняются."
    >
      <ul className="divide-y divide-slate-100">
        {models.map((model) => (
          <ModelRow
            key={model.name}
            model={model}
            busy={busyModel !== null}
            onDownload={() => onDownload(model.name)}
            onUseDefault={() => onUseDefault(model.name)}
            onDelete={() => onDelete(model.name)}
          />
        ))}
      </ul>
    </Card>
  );
}


/**
 * Human-readable reason for the computed concurrency.
 *
 * The planner already reports which resource bound the result, so the UI
 * explains it in words instead of leaving the user with a bare number.
 */
function planExplanation(plan, hardware) {
  if (!plan) return [];

  const lines = [];
  const effective = hardware?.cpu?.effective;
  const totalRam = hardware?.memory?.total_bytes;

  if (effective && totalRam) {
    lines.push(
      `Сервис обнаружил ${effective} доступных CPU-потоков и ${formatBytes(totalRam)} RAM.`,
    );
  }

  if (plan.reserved_cores > 0) {
    lines.push(
      `Режим использует ${plan.usable_cores} ядер, резервируя ${plan.reserved_cores} для ОС и других сервисов.`,
    );
  } else {
    lines.push(`Режим использует все ${plan.usable_cores} доступных ядер.`);
  }

  lines.push(
    `Бюджет памяти: ${formatMb(plan.ram_budget_mb)}, на одну задачу с моделью ${plan.whisper_model} — около ${formatMb(plan.ram_per_job_mb)}.`,
  );

  const reason = LIMIT_REASONS[plan.limited_by];
  if (reason) lines.push(reason);

  if (hardware && hardware.gpu && !hardware.gpu.available) {
    const torch = hardware.torch || {};
    if (!torch.installed) {
      lines.push("GPU не используется: PyTorch не установлен.");
    } else if (!torch.cuda_available && !torch.mps_available) {
      lines.push(
        "GPU не используется: текущая сборка PyTorch не поддерживает CUDA/MPS, транскрибация идёт на CPU.",
      );
    }
  }

  return lines;
}

function ProfileCard({ card, plan, active, busy, hardware, onSelect }) {
  return (
    <div
      className={`rounded-lg border p-4 transition ${
        active ? "border-slate-900 bg-slate-50" : "border-slate-200 bg-white"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-slate-900">{card.title}</h3>
            {active && <Badge tone="success">активен</Badge>}
          </div>
          <p className="mt-1 text-sm text-slate-500">{card.description}</p>
        </div>
        <Button
          variant={active ? "primary" : "secondary"}
          onClick={onSelect}
          disabled={busy || active}
          aria-pressed={active}
        >
          {active ? "Выбран" : "Выбрать"}
        </Button>
      </div>

      {plan ? (
        <>
          <dl className="mt-3 divide-y divide-slate-100 border-t border-slate-100 pt-1">
            <Row label="Устройство" value={(plan.device || "cpu").toUpperCase()} />
            <Row label="Одновременных транскрибаций" value={plan.max_concurrency} />
            <Row label="CPU-потоков на задачу" value={plan.torch_num_threads} />
            <Row label="Используется ядер" value={plan.usable_cores} />
            <Row label="Зарезервировано ядер" value={plan.reserved_cores} />
            <Row label="Модель" value={plan.whisper_model} />
            <Row
              label="Ограничивающий ресурс"
              value={plan.limited_by || "—"}
              hint={`cpu=${plan.cpu_concurrency} ram=${plan.ram_concurrency}`}
            />
          </dl>
          <ul className="mt-3 space-y-1 text-xs text-slate-500">
            {planExplanation(plan, hardware).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      ) : (
        <p className="mt-3 text-xs text-slate-400">Расчёт недоступен.</p>
      )}
    </div>
  );
}


export default function WhisperPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "ADMIN";

  const [hardware, setHardware] = useState(null);
  const [hardwareError, setHardwareError] = useState(null);
  const [models, setModels] = useState([]);
  const [modelsError, setModelsError] = useState(null);
  const [current, setCurrent] = useState(null);
  const [currentError, setCurrentError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busyModel, setBusyModel] = useState(null);
  const [profileBusy, setProfileBusy] = useState(false);
  const [actionError, setActionError] = useState(null);
  const [notice, setNotice] = useState(null);

  // Guards against a state update after unmount when a poll is in flight.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const data = await settingsApi.models();
      if (!mountedRef.current) return null;
      setModels(data.models || []);
      setModelsError(null);
      return data;
    } catch (error) {
      if (mountedRef.current) setModelsError(error);
      return null;
    }
  }, []);

  const loadCurrent = useCallback(async () => {
    try {
      const data = await settingsApi.current();
      if (!mountedRef.current) return null;
      setCurrent(data);
      setCurrentError(null);
      return data;
    } catch (error) {
      if (mountedRef.current) setCurrentError(error);
      return null;
    }
  }, []);

  const loadHardware = useCallback(async () => {
    try {
      const data = await settingsApi.hardware();
      if (!mountedRef.current) return null;
      setHardware(data);
      setHardwareError(null);
      return data;
    } catch (error) {
      if (mountedRef.current) setHardwareError(error);
      return null;
    }
  }, []);

  // Initial load. Each section fails independently so one broken endpoint does
  // not blank the whole page.
  useEffect(() => {
    if (!isAdmin) {
      setLoading(false);
      return;
    }
    (async () => {
      await Promise.all([loadHardware(), loadModels(), loadCurrent()]);
      if (mountedRef.current) setLoading(false);
    })();
  }, [isAdmin, loadHardware, loadModels, loadCurrent]);

  // Poll only while something is actually downloading; the backend exposes no
  // percentage, so this just flips the status from "loading" to "ready".
  const hasDownloading = models.some(
    (model) => model.status === "loading" || model.status === "downloading",
  );

  useEffect(() => {
    if (!hasDownloading || !isAdmin) return undefined;
    const timer = setInterval(() => {
      loadModels();
    }, DOWNLOAD_POLL_MS);
    return () => clearInterval(timer);
  }, [hasDownloading, isAdmin, loadModels]);


  const handleDownload = useCallback(
    async (name) => {
      setBusyModel(name);
      setActionError(null);
      try {
        await settingsApi.downloadModel(name);
        setNotice(`Скачивание модели ${name} запущено.`);
        await loadModels();
      } catch (error) {
        setActionError(error);
      } finally {
        if (mountedRef.current) setBusyModel(null);
      }
    },
    [loadModels],
  );

  const handleUseDefault = useCallback(
    async (name) => {
      setBusyModel(name);
      setActionError(null);
      try {
        await settingsApi.setDefaultModel(name);
        // Re-read both: the plan is model-aware, so switching the model changes
        // the recommended concurrency for both profiles.
        await Promise.all([loadModels(), loadCurrent()]);
        setNotice(
          `Модель ${name} выбрана по умолчанию. Новые настройки применяются к следующим задачам; уже выполняющаяся транскрибация завершается на старых параметрах.`,
        );
      } catch (error) {
        setActionError(error);
      } finally {
        if (mountedRef.current) setBusyModel(null);
      }
    },
    [loadModels, loadCurrent],
  );

  const handleDelete = useCallback(
    async (name) => {
      // eslint-disable-next-line no-alert
      const confirmed = window.confirm(`Удалить модель ${name} с диска?`);
      if (!confirmed) return;

      setBusyModel(name);
      setActionError(null);
      try {
        const result = await settingsApi.deleteModel(name);
        if (result && result.success === false) {
          // The endpoint reports a refusal in the body rather than as an HTTP
          // error (model in use, still downloading, not on disk).
          setActionError(new ApiError(409, "MODEL_DELETE_CONFLICT", result.error));
        } else {
          setNotice(`Модель ${name} удалена.`);
        }
        await loadModels();
      } catch (error) {
        setActionError(error);
      } finally {
        if (mountedRef.current) setBusyModel(null);
      }
    },
    [loadModels],
  );

  const handleProfile = useCallback(
    async (profile) => {
      setProfileBusy(true);
      setActionError(null);
      try {
        await settingsApi.setProfile(profile);
        await loadCurrent();
        setNotice(
          "Профиль сохранён. Новые настройки будут применены к следующим задачам; уже выполняющаяся транскрибация завершается на старых параметрах.",
        );
      } catch (error) {
        setActionError(error);
      } finally {
        if (mountedRef.current) setProfileBusy(false);
      }
    },
    [loadCurrent],
  );


  if (!isAdmin) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">Whisper</h1>
          <p className="mt-1 text-sm text-slate-500">
            Модель распознавания и производительность сервера.
          </p>
        </div>
        <Card>
          <EmptyState
            title="Доступно только администратору"
            description="Это глобальные настройки сервера, поэтому изменять их может только пользователь с ролью ADMIN."
          />
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Whisper</h1>
        <p className="mt-1 text-sm text-slate-500">
          Модель распознавания и производительность сервера.
        </p>
      </div>

      {actionError && (
        <ErrorBanner error={describeError(actionError)} onDismiss={() => setActionError(null)} />
      )}

      {notice && (
        <div
          role="status"
          className="flex items-start justify-between gap-3 rounded-md border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700"
        >
          <p>{notice}</p>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-xs text-slate-400 hover:text-slate-600"
            aria-label="Скрыть сообщение"
          >
            Скрыть
          </button>
        </div>
      )}

      <HardwareSection hardware={hardware} loading={loading} error={hardwareError} />

      <ModelsSection
        models={models}
        loading={loading}
        error={modelsError}
        busyModel={busyModel}
        onDownload={handleDownload}
        onUseDefault={handleUseDefault}
        onDelete={handleDelete}
      />

      <Card
        title="Производительность"
        description={
          current
            ? `Активный профиль: ${current.profile}. Модель по умолчанию: ${current.default_model}.`
            : "Расчёт распределения CPU и RAM."
        }
      >
        {currentError ? (
          <ErrorBanner error={describeError(currentError)} />
        ) : loading ? (
          <EmptyState title="Загрузка..." />
        ) : (
          <div className="grid gap-4 lg:grid-cols-2">
            {PROFILE_CARDS.map((card) => (
              <ProfileCard
                key={card.id}
                card={card}
                plan={current?.[card.planKey]}
                active={current?.profile === card.id}
                busy={profileBusy}
                hardware={hardware}
                onSelect={() => handleProfile(card.id)}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

