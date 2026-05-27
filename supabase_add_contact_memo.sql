alter table public.companies
add column if not exists contact_memo text not null default '';
