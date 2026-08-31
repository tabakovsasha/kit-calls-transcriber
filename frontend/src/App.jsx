import { useAuth } from "./auth/AuthContext";
import AppLayout from "./layout/AppLayout";
import ChangePasswordPage from "./pages/ChangePasswordPage";
import ConnectionsPage from "./pages/ConnectionsPage";
import LoginPage from "./pages/LoginPage";
import CallsPage from "./pages/CallsPage";
import QueuePage from "./pages/QueuePage";
import { SchedulesPage } from "./pages/PlaceholderPages";
import { EventsProvider } from "./realtime/EventsContext";
import { useRoute } from "./router";
import { CallsStateProvider } from "./state/CallsStateContext";
import { SelectedConnectionProvider, useSelectedConnection } from "./state/SelectedConnectionContext";

/**
 * Top-level gate.
 *
 * Four mutually exclusive states, decided by the auth context alone:
 *   restoring            -> waiting on the refresh-cookie probe
 *   not authenticated    -> login screen only
 *   must change password -> forced password change only
 *   authenticated        -> the routed application shell
 *
 * Routing is intentionally only reachable in the last state, so no page can
 * render before the session is known.
 */

const DEFAULT_ROUTE = "/calls";

const PAGES = {
  "/calls": CallsPage,
  "/queue": QueuePage,
  "/schedules": SchedulesPage,
  "/connections": ConnectionsPage,
};

export default function App() {
  const { status, isAuthenticated, mustChangePassword } = useAuth();

  if (status === "restoring") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50">
        <p className="text-sm text-slate-500">Загрузка...</p>
      </div>
    );
  }

  if (!isAuthenticated) return <LoginPage />;
  if (mustChangePassword) return <ChangePasswordPage />;

  return (
    <SelectedConnectionProvider>
      {/* One socket for the whole shell; pages subscribe through useEvents(). */}
      <EventsProvider>
        <AuthenticatedApp />
      </EventsProvider>
    </SelectedConnectionProvider>
  );
}

function AuthenticatedApp() {
  const { path } = useRoute(DEFAULT_ROUTE);
  const { selectedId } = useSelectedConnection();
  const Page = PAGES[path] ?? PAGES[DEFAULT_ROUTE];

  return (
    // Calls state lives above the router, so leaving /calls unmounts the page
    // but not its filters or loaded rows.
    <CallsStateProvider connectionId={selectedId}>
      <AppLayout currentPath={path}>
        <Page />
      </AppLayout>
    </CallsStateProvider>
  );
}
