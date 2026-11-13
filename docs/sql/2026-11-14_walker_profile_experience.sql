-- Perfil del paseador: años de experiencia y tamaños de perro que acepta.
-- El dueño los ve en la tarjeta de cada oferta (Explorar), junto a la
-- descripción, la zona y los paseos hechos. Ambos son opcionales.
-- Aplicar en Supabase (SQL editor) antes de usar el Perfil nuevo.
alter table public.walker_profiles
  add column if not exists experience_years smallint
    check (experience_years between 0 and 50),
  add column if not exists dog_sizes text[]
    check (dog_sizes <@ array['small', 'medium', 'large']::text[]);
