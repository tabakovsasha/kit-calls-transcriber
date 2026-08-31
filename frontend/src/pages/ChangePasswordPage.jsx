import { useState } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button, ErrorBanner, Field, TextInput } from "../components/ui";

/**
 * Forced password change after the first login.
 *
 * The backend blocks the rest of the API with PASSWORD_CHANGE_REQUIRED while
 * must_change_password is set, so this screen is the only reachable state.
 * Validation is still done server-side; the client-side check below only avoids
 * an obviously pointless round trip.
 */
const MIN_LENGTH = 10;

export default function ChangePasswordPage() {
  const { user, changePassword, logout } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event) {
    event.preventDefault();
    setError(null);

    if (newPassword !== confirmPassword) {
      setError({ message: "Новый пароль и подтверждение не совпадают" });
      return;
    }
    if (newPassword.length < MIN_LENGTH) {
      setError({ message: `Пароль должен содержать минимум ${MIN_LENGTH} символов` });
      return;
    }

    setBusy(true);
    try {
      await changePassword(currentPassword, newPassword);
    } catch (submitError) {
      setError(
        submitError instanceof ApiError
          ? submitError
          : { message: "Не удалось сменить пароль" },
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <h1 className="text-xl font-semibold text-slate-900">Смена пароля</h1>
          <p className="mt-1 text-sm text-slate-500">
            Для {user?.email} требуется задать новый пароль перед началом работы.
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="space-y-4 rounded-lg border border-slate-200 bg-white px-6 py-6 shadow-sm"
        >
          <ErrorBanner error={error} onDismiss={() => setError(null)} />

          <Field label="Текущий пароль">
            <TextInput
              type="password"
              autoComplete="current-password"
              required
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
          </Field>

          <Field label="Новый пароль" hint={`Минимум ${MIN_LENGTH} символов`}>
            <TextInput
              type="password"
              autoComplete="new-password"
              required
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
          </Field>

          <Field label="Подтверждение нового пароля">
            <TextInput
              type="password"
              autoComplete="new-password"
              required
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
            />
          </Field>

          <div className="flex items-center gap-2">
            <Button type="submit" disabled={busy} className="flex-1">
              {busy ? "Сохранение..." : "Сменить пароль"}
            </Button>
            <Button variant="secondary" onClick={logout} disabled={busy}>
              Выйти
            </Button>
          </div>

          <p className="text-xs text-slate-500">
            После смены пароля все другие сессии будут завершены.
          </p>
        </form>
      </div>
    </main>
  );
}
