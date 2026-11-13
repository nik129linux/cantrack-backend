-- Perfil del dueño: recogida, emergencia, veterinario, pago, avisos y favoritos.
-- Aplicar en Supabase (SQL editor) antes de usar las secciones nuevas del perfil.
alter table public.owner_profiles
  add column if not exists pickup_notes text check (char_length(pickup_notes) <= 300),
  add column if not exists handoff_method text check (handoff_method in ('in_person','doorman','key')),
  add column if not exists emergency_name text check (char_length(emergency_name) <= 80),
  add column if not exists emergency_relation text check (char_length(emergency_relation) <= 40),
  add column if not exists emergency_phone text check (char_length(emergency_phone) <= 20),
  add column if not exists vet_name text check (char_length(vet_name) <= 80),
  add column if not exists vet_phone text check (char_length(vet_phone) <= 20),
  add column if not exists payment_method text check (payment_method in ('cash','nequi','card')),
  add column if not exists notify_prefs jsonb not null
    default '{"walkStarted":true,"report":true,"walkDone":true,"offers":true}'::jsonb,
  add column if not exists favorite_walker_ids uuid[] not null default '{}';
