import { useState } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Button, ErrorBanner, Field, TextInput } from "../components/ui";

export default function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email.trim(), password);
      // On success the shell replaces this screen; the local password state is
      // discarded with the unmounted component and is never persisted.
    } catch (submitError) {
      setError(
        submitError instanceof ApiError
          ? submitError
          : { message: "Сервис недоступен, попробуйте позже" },
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-xl font-semibold text-slate-900">Kit Calls Transcriber</h1>
          <p className="mt-1 text-sm text-slate-500">Вход в систему</p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="space-y-4 rounded-lg border border-slate-200 bg-white px-6 py-6 shadow-sm"
        >
          <ErrorBanner error={error} onDismiss={() => setError(null)} />

          <Field label="Email">
            <TextInput
              type="email"
              name="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="admin@example.com"
            />
          </Field>

          <Field label="Пароль">
            <TextInput
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>

          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "Вход..." : "Войти"}
          </Button>
        </form>
      </div>
    </main>
  );
}
