import { useAuth } from "../auth/AuthContext";
import { useEvents } from "../realtime/EventsContext";
import { Badge, Button } from "../components/ui";
import { Link } from "../router";

/**
 * Authenticated application shell.
 *
 * The websocket itself is owned by EventsProvider (mounted above this shell) so
 * it survives navigation between sections; here we only read its status for the
 * indicator.
 */

const NAV_ITEMS = [
  { to: "/calls", label: "Звонки" },
  { to: "/queue", label: "Очередь" },
  { to: "/schedules", label: "Расписания" },
  { to: "/connections", label: "Подключения" },
  // Server-wide Whisper settings: ADMIN only, so the item is hidden for USER.
  { to: "/whisper", label: "Whisper", adminOnly: true },
];

const SOCKET_LABELS = {
  idle: { text: "офлайн", tone: "neutral" },
  connecting: { text: "подключение", tone: "warning" },
  reconnecting: { text: "переподключение", tone: "warning" },
  open: { text: "онлайн", tone: "success" },
  rejected: { text: "отказано", tone: "danger" },
};

export default function AppLayout({ currentPath, children }) {
  const { user, logout } = useAuth();
  const { socketState, lastEventAt } = useEvents();
  const socket = SOCKET_LABELS[socketState] ?? SOCKET_LABELS.idle;

  // Server-wide sections are hidden for non-admins; the backend rejects them
  // anyway, so showing the entry would only advertise a dead end.
  const isAdmin = user?.role === "ADMIN";
  const navItems = NAV_ITEMS.filter((item) => !item.adminOnly || isAdmin);

  const navClass = (to, active) =>
    `block rounded-md px-3 py-2 text-sm font-medium transition ${
      to === active ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"
    }`;

  return (
    <div className="flex min-h-screen bg-slate-50">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-slate-200 bg-white md:flex">
        <div className="border-b border-slate-200 px-5 py-4">
          <p className="text-sm font-semibold text-slate-900">Kit Calls Transcriber</p>
          <p className="mt-0.5 text-xs text-slate-500">Транскрибация звонков</p>
        </div>

        <nav className="flex-1 px-3 py-4" aria-label="Основная навигация">
          <ul className="space-y-1">
            {navItems.map((item) => (
              <li key={item.to}>
                <Link
                  to={item.to}
                  className={navClass(item.to, currentPath)}
                  aria-current={item.to === currentPath ? "page" : undefined}
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>

        <div className="border-t border-slate-200 px-5 py-3 text-xs text-slate-500">
          <div className="flex items-center gap-2">
            <span>Realtime:</span>
            <Badge tone={socket.tone}>{socket.text}</Badge>
          </div>
          {lastEventAt && (
            <p className="mt-1">Событие: {lastEventAt.toLocaleTimeString("ru-RU")}</p>
          )}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-6 py-3">
          <nav className="flex gap-1 md:hidden" aria-label="Навигация">
            {navItems.map((item) => (
              <Link
                key={item.to}
                to={item.to}
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  item.to === currentPath ? "bg-slate-900 text-white" : "text-slate-600"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-4">
            <div className="text-right">
              <p className="text-sm font-medium text-slate-900">
                {user?.profile?.display_name || user?.email}
              </p>
              <p className="text-xs text-slate-500">
                {user?.email} · {user?.role}
              </p>
            </div>
            <Button variant="secondary" onClick={logout}>
              Выйти
            </Button>
          </div>
        </header>

        <main className="flex-1 px-6 py-6">{children}</main>
      </div>
    </div>
  );
}
