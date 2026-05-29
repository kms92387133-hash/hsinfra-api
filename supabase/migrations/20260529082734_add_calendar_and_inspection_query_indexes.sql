create index if not exists calendar_events_visible_range_idx
  on public.calendar_events(calendar_scope, deleted, start_at, end_at);

create index if not exists calendar_events_sync_status_idx
  on public.calendar_events(sync_status);

create index if not exists calendar_events_last_synced_at_idx
  on public.calendar_events(last_synced_at desc);

create index if not exists inspections_company_date_idx
  on public.inspections(company_id, date desc);