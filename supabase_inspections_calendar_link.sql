-- Link inspection rows to Synology Calendar CalDAV events.
-- Run this in Supabase SQL Editor before deploying the backend change.

alter table public.inspections
  add column if not exists calendar_event_uid text not null default '',
  add column if not exists calendar_scope text not null default 'company_shared',
  add column if not exists calendar_url text not null default '',
  add column if not exists calendar_href text not null default '';

create index if not exists inspections_calendar_event_uid_idx
  on public.inspections(calendar_event_uid);

create index if not exists inspections_calendar_href_idx
  on public.inspections(calendar_href);

create index if not exists inspections_calendar_scope_idx
  on public.inspections(calendar_scope);
