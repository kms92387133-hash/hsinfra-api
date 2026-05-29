create table if not exists public.calendar_events (
  id uuid primary key default gen_random_uuid(),
  uid text not null default '',
  href text not null default '',
  etag text not null default '',
  calendar_scope text not null default 'company_shared',
  calendar_url text not null default '',
  calendar_name text not null default '',
  company_name text not null default '',
  title text not null default '',
  description text not null default '',
  start_at timestamptz not null,
  end_at timestamptz not null,
  location text not null default '',
  inspector text not null default '',
  inspectors jsonb not null default '[]'::jsonb,
  attendees jsonb not null default '[]'::jsonb,
  inspection_id uuid references public.inspections(id) on delete set null,
  can_edit boolean not null default true,
  all_day boolean not null default false,
  sync_status text not null default 'synced',
  last_synced_at timestamptz not null default now(),
  deleted boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.calendar_events
  add column if not exists uid text not null default '',
  add column if not exists href text not null default '',
  add column if not exists etag text not null default '',
  add column if not exists calendar_scope text not null default 'company_shared',
  add column if not exists calendar_url text not null default '',
  add column if not exists calendar_name text not null default '',
  add column if not exists company_name text not null default '',
  add column if not exists title text not null default '',
  add column if not exists description text not null default '',
  add column if not exists start_at timestamptz,
  add column if not exists end_at timestamptz,
  add column if not exists location text not null default '',
  add column if not exists inspector text not null default '',
  add column if not exists inspectors jsonb not null default '[]'::jsonb,
  add column if not exists attendees jsonb not null default '[]'::jsonb,
  add column if not exists inspection_id uuid references public.inspections(id) on delete set null,
  add column if not exists can_edit boolean not null default true,
  add column if not exists all_day boolean not null default false,
  add column if not exists sync_status text not null default 'synced',
  add column if not exists last_synced_at timestamptz not null default now(),
  add column if not exists deleted boolean not null default false,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

create unique index if not exists calendar_events_calendar_url_uid_unique_idx
  on public.calendar_events(calendar_url, uid)
  where uid <> '';

create unique index if not exists calendar_events_href_unique_idx
  on public.calendar_events(href)
  where href <> '';

create index if not exists calendar_events_scope_range_idx
  on public.calendar_events(calendar_scope, start_at, end_at);

create index if not exists calendar_events_deleted_idx
  on public.calendar_events(deleted);

create index if not exists calendar_events_inspection_id_idx
  on public.calendar_events(inspection_id);

create table if not exists public.calendar_sync_runs (
  id uuid primary key default gen_random_uuid(),
  status text not null default 'success',
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  scopes text not null default 'personal,company_shared',
  start_at timestamptz,
  end_at timestamptz,
  inserted_count integer not null default 0,
  updated_count integer not null default 0,
  deleted_count integer not null default 0,
  error_message text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists calendar_sync_runs_started_at_idx
  on public.calendar_sync_runs(started_at desc);

create index if not exists calendar_sync_runs_status_idx
  on public.calendar_sync_runs(status);

create table if not exists public.inspection_revision_conflicts (
  id uuid primary key default gen_random_uuid(),
  inspection_id uuid references public.inspections(id) on delete set null,
  attempted_revision integer not null default 0,
  current_revision integer not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists inspection_revision_conflicts_created_at_idx
  on public.inspection_revision_conflicts(created_at desc);

create index if not exists inspection_revision_conflicts_inspection_id_idx
  on public.inspection_revision_conflicts(inspection_id);
