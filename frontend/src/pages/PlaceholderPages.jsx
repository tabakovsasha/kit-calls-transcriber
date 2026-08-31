import { Card, EmptyState } from "../components/ui";

/**
 * Placeholder sections.
 *
 * These exist so navigation and the shell are testable now; the real Queue and
 * Schedules UIs are separate stages and are deliberately not stubbed with fake
 * data here. Calls is implemented in CallsPage.jsx.
 */

export function QueuePage() {
  return (
    <PlaceholderPage
      title="Очередь"
      description="Очередь задач транскрибации."
      pending="Список задач, прогресс в реальном времени, отмена и повтор."
    />
  );
}

export function SchedulesPage() {
  return (
    <PlaceholderPage
      title="Расписания"
      description="Периодическая автоматическая транскрибация."
      pending="Создание и управление расписаниями, история запусков."
    />
  );
}

function PlaceholderPage({ title, description, pending }) {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">{title}</h1>
        <p className="mt-1 text-sm text-slate-500">{description}</p>
      </div>
      <Card>
        <EmptyState title="Раздел в разработке" description={pending} />
      </Card>
    </div>
  );
}
