alter table public.companies
  add column if not exists contract_manager text not null default '',
  add column if not exists contract_phone text not null default '';
