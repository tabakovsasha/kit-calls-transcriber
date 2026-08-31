import { Card, EmptyState } from "../components/ui";

/**
 * Placeholder sections.
 *
 * Only Schedules remains a placeholder; it is a separate stage. Calls is
 * implemented in CallsPage.jsx and Queue in QueuePage.jsx.
 */

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
