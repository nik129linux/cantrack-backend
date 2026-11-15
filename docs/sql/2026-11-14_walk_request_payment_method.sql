-- How the owner will pay a walk, chosen when publishing the request (InDrive style).
alter table public.walk_requests add column if not exists payment_method text check (payment_method in ('cash','nequi','card'));
comment on column public.walk_requests.payment_method is 'How the owner will pay this walk (cash, nequi, card); shown to walkers on the request card.';
