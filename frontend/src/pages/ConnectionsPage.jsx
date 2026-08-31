import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../api/client";
import { connectionsApi } from "../api/endpoints";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorBanner,
  Field,
  TextInput,
} from "../components/ui";
import { useSelectedConnection } from "../state/SelectedConnectionContext";

/**
 * Voximplant connections.
 *
 * The access token is write-only end to end: it is typed here, sent once to
 * POST /api/connections (or the rotate-token endpoint) and then dropped from
 * component state. The backend only ever returns a mask plus a fingerprint, so
 * there is nothing secret to keep in the browser.
 */

const STATUS_TONES = {
  connected: "success",
  error: "danger",
  unauthorized: "danger",
  unknown: "neutral",
};

const EMPTY_FORM = {
  label: "",
  apiHost: "kitapi-eu.voximplant.com",
  domain: "",
  accessToken: "",
  isDefault: false,
  verify: true,
};

function StatusBadge({ status }) {
  return <Badge tone={STATUS_TONES[status] ?? "neutral"}>{status}</Badge>;
}

function formatDate(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU");
}

function ConnectionRow({
  connection,
  isSelected,
  isBusy,
  editing,
  onSelect,
  onVerify,
  onToggleEdit,
  onEditChange,
  onSubmitEdit,
  onRotate,
  onDelete,
}) {
  return (
    <li className="py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-slate-900">{connection.label}</span>
            <StatusBadge status={connection.last_connection_status} />
            {connection.is_default && <Badge tone="neutral">по умолчанию</Badge>}
            {!connection.is_active && <Badge tone="warning">выключено</Badge>}
            {isSelected && <Badge tone="success">выбрано</Badge>}
          </div>

          <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs text-slate-500 sm:grid-cols-2">
            <div>
              <dt className="inline font-medium">Domain: </dt>
              <dd className="inline">{connection.domain}</dd>
            </div>
            <div>
              <dt className="inline font-medium">API host: </dt>
              <dd className="inline">{connection.api_host}</dd>
            </div>
            <div>
              {/* Mask + fingerprint only: the value never leaves the server. */}
              <dt className="inline font-medium">Token: </dt>
              <dd className="inline">
                {connection.access_token_masked || "—"}
                {connection.access_token_fingerprint
                  ? ` (${connection.access_token_fingerprint})`
                  : ""}
              </dd>
            </div>
            <div>
              <dt className="inline font-medium">Проверено: </dt>
              <dd className="inline">{formatDate(connection.last_checked_at)}</dd>
            </div>
            {connection.account_name && (
              <div>
                <dt className="inline font-medium">Аккаунт: </dt>
                <dd className="inline">{connection.account_name}</dd>
              </div>
            )}
          </dl>

          {connection.last_connection_error && (
            <p className="mt-2 text-xs text-red-600">{connection.last_connection_error}</p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button variant={isSelected ? "primary" : "secondary"} onClick={onSelect} disabled={isBusy}>
            {isSelected ? "Выбрано" : "Выбрать"}
          </Button>
          <Button variant="secondary" onClick={onVerify} disabled={isBusy}>
            Проверить
          </Button>
          <Button variant="secondary" onClick={onToggleEdit} disabled={isBusy}>
            Изменить
          </Button>
          <Button variant="secondary" onClick={onRotate} disabled={isBusy}>
            Сменить токен
          </Button>
          <Button variant="danger" onClick={onDelete} disabled={isBusy}>
            Удалить
          </Button>
        </div>
      </div>

      {editing && (
        <form
          onSubmit={onSubmitEdit}
          className="mt-4 grid gap-4 rounded-md bg-slate-50 p-4 md:grid-cols-2"
        >
          <Field label="Название">
            <TextInput
              required
              value={editing.label}
              onChange={(event) => onEditChange({ ...editing, label: event.target.value })}
            />
          </Field>
          <Field label="API host">
            <TextInput
              required
              value={editing.api_host}
              onChange={(event) => onEditChange({ ...editing, api_host: event.target.value })}
            />
          </Field>
          <Field label="Domain">
            <TextInput
              required
              value={editing.domain}
              onChange={(event) => onEditChange({ ...editing, domain: event.target.value })}
            />
          </Field>

          <div className="flex flex-wrap items-center gap-5 md:col-span-2">
            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-slate-300"
                checked={editing.is_active}
                onChange={(event) =>
                  onEditChange({ ...editing, is_active: event.target.checked })
                }
              />
              Активно
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-slate-300"
                checked={editing.is_default}
                onChange={(event) =>
                  onEditChange({ ...editing, is_default: event.target.checked })
                }
              />
              По умолчанию
            </label>

            <div className="ml-auto flex gap-2">
              <Button variant="secondary" onClick={() => onEditChange(null)}>
                Отмена
              </Button>
              <Button type="submit" disabled={isBusy}>
                Сохранить
              </Button>
            </div>
          </div>
        </form>
      )}
    </li>
  );
}

export default function ConnectionsPage() {
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [creating, setCreating] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const [editing, setEditing] = useState(null);

  const { selectedId, select } = useSelectedConnection();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const items = await connectionsApi.list();
      setConnections(items);
      setError(null);
    } catch (loadError) {
      setError(
        loadError instanceof ApiError
          ? loadError
          : { message: "Не удалось загрузить подключения" },
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function reportError(operationError, fallback) {
    setError(operationError instanceof ApiError ? operationError : { message: fallback });
  }

  async function handleCreate(event) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setCreating(true);
    try {
      const created = await connectionsApi.create(form);
      // Resetting the form is what drops the plaintext token from memory.
      setForm(EMPTY_FORM);
      setNotice(`Подключение "${created.label}" создано`);
      await load();
    } catch (createError) {
      reportError(createError, "Не удалось создать подключение");
    } finally {
      setCreating(false);
    }
  }

  async function handleUpdate(event) {
    event.preventDefault();
    if (!editing) return;
    setError(null);
    setBusyId(editing.id);
    try {
      await connectionsApi.update(editing.id, {
        label: editing.label,
        api_host: editing.api_host,
        domain: editing.domain,
        is_active: editing.is_active,
        is_default: editing.is_default,
      });
      setEditing(null);
      setNotice("Подключение обновлено");
      await load();
    } catch (updateError) {
      reportError(updateError, "Не удалось обновить подключение");
    } finally {
      setBusyId(null);
    }
  }

  async function handleVerify(connection) {
    setError(null);
    setNotice(null);
    setBusyId(connection.id);
    try {
      const result = await connectionsApi.verify(connection.id);
      setNotice(
        result.error
          ? `Проверка не прошла: ${result.error}`
          : `Проверка выполнена: ${result.status}`,
      );
      await load();
    } catch (verifyError) {
      reportError(verifyError, "Не удалось проверить подключение");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(connection) {
    // Soft-delete on the backend, which also scrubs the stored token.
    const confirmed = window.confirm(
      `Удалить подключение "${connection.label}"? Сохраненный токен будет уничтожен.`,
    );
    if (!confirmed) return;

    setError(null);
    setBusyId(connection.id);
    try {
      await connectionsApi.remove(connection.id);
      if (selectedId === connection.id) select(null);
      setNotice("Подключение удалено");
      await load();
    } catch (deleteError) {
      reportError(deleteError, "Не удалось удалить подключение");
    } finally {
      setBusyId(null);
    }
  }

  async function handleRotate(connection) {
    const token = window.prompt(`Новый access token для "${connection.label}"`);
    if (!token) return;

    setError(null);
    setBusyId(connection.id);
    try {
      await connectionsApi.rotateToken(connection.id, token);
      setNotice("Токен обновлен");
      await load();
    } catch (rotateError) {
      reportError(rotateError, "Не удалось обновить токен");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Подключения</h1>
        <p className="mt-1 text-sm text-slate-500">
          Учетные данные Voximplant Kit хранятся только на сервере в зашифрованном виде.
        </p>
      </div>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {notice && (
        <div
          role="status"
          className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800"
        >
          {notice}
        </div>
      )}

      <Card
        title="Новое подключение"
        description="Access token отправляется на сервер один раз и не сохраняется в браузере."
      >
        <form onSubmit={handleCreate} className="grid gap-4 md:grid-cols-2">
          <Field label="Название">
            <TextInput
              required
              value={form.label}
              onChange={(event) => setForm({ ...form, label: event.target.value })}
              placeholder="Основной аккаунт"
            />
          </Field>

          <Field label="API host">
            <TextInput
              required
              value={form.apiHost}
              onChange={(event) => setForm({ ...form, apiHost: event.target.value })}
            />
          </Field>

          <Field label="Domain">
            <TextInput
              required
              value={form.domain}
              onChange={(event) => setForm({ ...form, domain: event.target.value })}
              placeholder="mycompany.voximplant.com"
            />
          </Field>

          <Field label="Access token" hint="Минимум 8 символов">
            <TextInput
              type="password"
              required
              minLength={8}
              autoComplete="off"
              value={form.accessToken}
              onChange={(event) => setForm({ ...form, accessToken: event.target.value })}
            />
          </Field>

          <div className="flex flex-wrap items-center gap-5 md:col-span-2">
            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-slate-300"
                checked={form.isDefault}
                onChange={(event) => setForm({ ...form, isDefault: event.target.checked })}
              />
              Сделать основным
            </label>

            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-slate-300"
                checked={form.verify}
                onChange={(event) => setForm({ ...form, verify: event.target.checked })}
              />
              Проверить подключение при создании
            </label>

            <Button type="submit" disabled={creating} className="ml-auto">
              {creating ? "Создание..." : "Создать подключение"}
            </Button>
          </div>
        </form>
      </Card>

      <Card
        title="Список подключений"
        actions={
          <Button variant="secondary" onClick={load} disabled={loading}>
            Обновить
          </Button>
        }
      >
        {loading && <p className="text-sm text-slate-500">Загрузка...</p>}

        {!loading && connections.length === 0 && (
          <EmptyState
            title="Подключений пока нет"
            description="Добавьте первое подключение Voximplant Kit, чтобы загружать звонки."
          />
        )}

        {!loading && connections.length > 0 && (
          <ul className="divide-y divide-slate-200">
            {connections.map((connection) => (
              <ConnectionRow
                key={connection.id}
                connection={connection}
                isSelected={selectedId === connection.id}
                isBusy={busyId === connection.id}
                editing={editing?.id === connection.id ? editing : null}
                onSelect={() => select(selectedId === connection.id ? null : connection.id)}
                onVerify={() => handleVerify(connection)}
                onToggleEdit={() =>
                  setEditing(editing?.id === connection.id ? null : { ...connection })
                }
                onEditChange={setEditing}
                onSubmitEdit={handleUpdate}
                onRotate={() => handleRotate(connection)}
                onDelete={() => handleDelete(connection)}
              />
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
