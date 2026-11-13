-- Notas privadas del paseador sobre el perro de un cliente ("el portero no abre
-- antes de las 7"). Solo las lee y las escribe quien las guarda: el API entra con
-- la llave de servicio y la pantalla de Clientes nunca las muestra al dueño.
-- Aplicar en Supabase (SQL editor) antes de usar la ficha del cliente.
create table if not exists public.walker_dog_notes (
  walker_id uuid not null references auth.users (id) on delete cascade,
  dog_id uuid not null references public.dogs (id) on delete cascade,
  note text check (char_length(note) <= 500),
  updated_at timestamptz not null default now(),
  primary key (walker_id, dog_id)
);

-- deny-all como el resto del esquema: sin políticas, solo el service-role entra.
alter table public.walker_dog_notes enable row level security;
