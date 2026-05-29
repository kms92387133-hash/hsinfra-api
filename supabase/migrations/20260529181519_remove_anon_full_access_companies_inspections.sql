begin;

alter table public.companies enable row level security;
alter table public.inspections enable row level security;

drop policy if exists "Allow all companies" on public.companies;
drop policy if exists "Allow all inspections" on public.inspections;

commit;
