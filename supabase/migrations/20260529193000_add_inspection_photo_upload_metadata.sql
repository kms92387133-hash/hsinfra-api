alter table public.inspection_photos
  add column if not exists local_path text not null default '',
  add column if not exists local_filename text not null default '',
  add column if not exists nas_folder text not null default '',
  add column if not exists nas_subfolder text not null default '',
  add column if not exists nas_filename text not null default '',
  add column if not exists upload_status text not null default 'not_uploaded',
  add column if not exists upload_error text not null default '',
  add column if not exists uploaded_at timestamptz;

alter table public.inspection_photos
  add constraint inspection_photos_upload_status_v1_check
  check (upload_status in ('not_uploaded', 'uploading', 'uploaded', 'failed'));

create index if not exists inspection_photos_upload_status_idx
  on public.inspection_photos(upload_status);

create index if not exists inspection_photos_nas_lookup_idx
  on public.inspection_photos(nas_folder, nas_subfolder, nas_filename);
