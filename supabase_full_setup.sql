-- HS inspection app Supabase baseline schema.
-- Run this in Supabase SQL Editor.

create extension if not exists pgcrypto;

create table if not exists public.companies (
  id uuid primary key default gen_random_uuid(),
  company_name text not null,
  address text not null default '',
  address_group text not null default '',
  building_type text not null default '',
  manager text not null default '',
  phone text not null default '',
  contact_memo text not null default '',
  created_at timestamptz not null default now()
);

alter table public.companies
  add column if not exists company_name text not null default '',
  add column if not exists address text not null default '',
  add column if not exists address_group text not null default '',
  add column if not exists building_type text not null default '',
  add column if not exists manager text not null default '',
  add column if not exists phone text not null default '',
  add column if not exists contact_memo text not null default '',
  add column if not exists created_at timestamptz not null default now();

create unique index if not exists companies_company_name_unique_idx
  on public.companies(company_name);

create index if not exists companies_address_group_idx
  on public.companies(address_group);

create index if not exists companies_building_type_idx
  on public.companies(building_type);

create table if not exists public.inspections (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references public.companies(id) on delete set null,
  date date not null,
  category text not null default '유지보수',
  created_at timestamptz not null default now()
);

alter table public.inspections
  add column if not exists company_id uuid references public.companies(id) on delete set null,
  add column if not exists date date,
  add column if not exists category text not null default '유지보수',
  add column if not exists created_at timestamptz not null default now();

create index if not exists inspections_company_id_idx
  on public.inspections(company_id);

create index if not exists inspections_date_idx
  on public.inspections(date);

create index if not exists inspections_category_idx
  on public.inspections(category);

create table if not exists public.inspection_photos (
  id uuid primary key default gen_random_uuid(),
  inspection_id uuid not null references public.inspections(id) on delete cascade,
  facility_name text not null default '',
  photo_title text not null default '',
  file_name text not null default '',
  storage_path text not null default '',
  sort_order integer not null default 0,
  uploaded_to_nas boolean not null default false,
  created_at timestamptz not null default now()
);

alter table public.inspection_photos
  add column if not exists inspection_id uuid references public.inspections(id) on delete cascade,
  add column if not exists facility_name text not null default '',
  add column if not exists photo_title text not null default '',
  add column if not exists file_name text not null default '',
  add column if not exists storage_path text not null default '',
  add column if not exists sort_order integer not null default 0,
  add column if not exists uploaded_to_nas boolean not null default false,
  add column if not exists created_at timestamptz not null default now();

create index if not exists inspection_photos_inspection_id_idx
  on public.inspection_photos(inspection_id);

create index if not exists inspection_photos_storage_path_idx
  on public.inspection_photos(storage_path);

create unique index if not exists inspection_photos_unique_slot_idx
  on public.inspection_photos(inspection_id, facility_name, sort_order);

create table if not exists public.inspection_schedules (
  id uuid primary key default gen_random_uuid(),
  company_id uuid references public.companies(id) on delete set null,
  date date not null,
  category text not null default '유지보수',
  time text not null default '',
  created_at timestamptz not null default now()
);

alter table public.inspection_schedules
  add column if not exists company_id uuid references public.companies(id) on delete set null,
  add column if not exists date date,
  add column if not exists category text not null default '유지보수',
  add column if not exists time text not null default '',
  add column if not exists created_at timestamptz not null default now();

create index if not exists inspection_schedules_company_id_idx
  on public.inspection_schedules(company_id);

create index if not exists inspection_schedules_date_idx
  on public.inspection_schedules(date);

create index if not exists inspection_schedules_category_idx
  on public.inspection_schedules(category);

-- Optional compatibility: convert old category values to the current app label.
update public.inspections
set category = '유지보수'
where category = '유지점검';

update public.inspection_schedules
set category = '유지보수'
where category = '유지점검';

-- Optional legacy Supabase Storage bucket.
-- The current production direction stores photo files in Synology NAS,
-- while Supabase keeps metadata only. Enable this only if you still use
-- the old Supabase Storage upload flow.
--
-- insert into storage.buckets (id, name, public)
-- values ('inspection-photos', 'inspection-photos', false)
-- on conflict (id) do nothing;
