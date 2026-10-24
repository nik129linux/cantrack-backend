# Frontend changes for PR0 (security foundation)

The backend now ignores any role the browser writes. Three things in the web app must change.

## 1. OAuth signup: stop calling `updateUser`, call `POST /auth/role`

`App.tsx` currently applies the role picked before a Google/Apple redirect with
`supabase.auth.updateUser({ data: { role } })`. That writes `user_metadata`, which the API
no longer reads (any user can write it, so it proves nothing).

Replace the effect with one API call, made once, right after the first session arrives:

```ts
// POST /auth/role  — bearer token required, rate-limited to 5/min
// body:    { "role": "owner" | "walker" }
// 200:     { "role": "owner" }                 -> role stored, reload the session
// 409:     { "detail": "This account already has a role." }  -> ignore, already set
// 400:     invalid role                         -> bug in our code, log it
// 503:     auth provider down                   -> show the retry banner below
```

After a 200, call `supabase.auth.refreshSession()` so the new `app_metadata.role` is in the
token, then clear `localStorage["cantrack.pendingRole"]`.

The email/password signup needs no change: `POST /auth/signup` stores the role server-side.

## 2. Read the role from `app_metadata`

Wherever the app decides which shell to show, read `session.user.app_metadata.role`, not
`user_metadata.role`. An account with no `app_metadata.role` is the OAuth case above: show the
role picker, not the "no role" error.

## 3. New error shapes

| Status | Body | UI copy (es) |
|---|---|---|
| 400 validation | `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` (no `input` any more) | field message from `msg` |
| 401 | `{"detail": "Invalid login credentials."}` | "Correo o contraseña incorrectos." |
| 404 | `{"detail": "We couldn't find what you're looking for."}` | "No encontramos lo que buscas." |
| 409 | `{"detail": "This changed while you were acting on it. Reload and try again."}` | "Esto cambió mientras lo hacías. Recarga e inténtalo otra vez." |
| 413 | `{"detail": "The request is too large."}` | "El archivo es demasiado grande." |
| 429 | `{"detail": "Too many requests. Try again in a minute."}` + `Retry-After` header | "Vas muy rápido. Intenta en un minuto." (disable the button for `Retry-After` seconds) |
| 500 | `{"detail": "Something went wrong on our side.", "errorId": "…"}` | "Algo falló de nuestro lado. Código: {errorId}" |
| 503 | `{"detail": "..."}` | "El servicio no responde. Intenta de nuevo." |

`api.ts` already strips the `body`/`query` prefix from `loc`; that keeps working.

## Limits the forms should enforce too (the API rejects anything above)

| Field | Max |
|---|---|
| Dog name / breed | 80 |
| Dog notes | 1000 |
| Walker display name / bio / service area | 80 / 500 / 120 |
| Walk price (`priceCop`) | 1,000,000 COP |
| Offer ETA | 240 min |
| Walk window start | within the next 30 days |

## Never do

- Call Supabase tables from the browser. Every table is deny-all now: the call returns nothing.
  Only `supabase.auth` (login, session, OAuth) is allowed in the web app.
- Call `/_whoami`: it was removed. The user's id and email are already in the Supabase session.
