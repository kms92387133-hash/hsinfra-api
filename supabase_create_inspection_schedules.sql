create table if not exists public.inspection_schedules (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references public.companies(id) on delete set null,
  date date not null,
  category text not null default '유지보수',
  time text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists inspection_schedules_company_id_idx
  on public.inspection_schedules(company_id);

create index if not exists inspection_schedules_date_idx
  on public.inspection_schedules(date);
