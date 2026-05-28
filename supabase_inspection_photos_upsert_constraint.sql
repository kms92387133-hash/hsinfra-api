create unique index if not exists inspection_photos_unique_slot_idx
  on public.inspection_photos(inspection_id, facility_name, sort_order);
