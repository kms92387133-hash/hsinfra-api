alter table public.companies
  add column if not exists third_manager text not null default '',
  add column if not exists third_phone text not null default '';
