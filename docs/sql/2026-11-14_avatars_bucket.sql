-- Profile photos (owner and walker Perfil) live in a PRIVATE bucket the API
-- writes with the service-role key and serves only as one-hour signed URLs
-- (routers/profile.py and routers/walker_profile.py, AVATAR_BUCKET = "avatars").
-- Without this bucket every "Cambiar foto" fails with a 500 ("Bucket not found").
-- Applied to the Supabase project on 2026-11-14.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('avatars', 'avatars', false, 2097152, array['image/webp', 'image/jpeg', 'image/png'])
on conflict (id) do nothing;
