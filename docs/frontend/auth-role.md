# Frontend brief: accounts and sessions

Covers signup, email verification, login, Google, sessions, password change and sign-out.
**This replaces the previous version, which said the email signup needed no change. It does.**

## The rule

The browser never creates accounts or logs in **directly** against Supabase any more.
- **Signup and login go through the API**, which assigns the role server-side and applies the lockout and the password policy. The app then hands the tokens to supabase-js so it can keep the session fresh.
- **supabase-js is still used for:** keeping and refreshing the session, the Google redirect, and the password-recovery link.

Today `AuthScreens.tsx` calls `supabase.auth.signUp` and `supabase.auth.signInWithPassword` directly.
- **Effect:** every new account is created **without a role**, so the API refuses it with 400/403.
- **Fix:** replace both calls with the ones below.

## Signup: `POST /auth/signup`

```json
{"email": "camila@gmail.com", "password": "mi perro se llama luna", "role": "owner"}
```

- **201** → `{"user": {...}, "session": null}`. Email confirmation is on, so there is no session yet. Show the screen "Revisa tu correo: te enviamos un link para activar la cuenta", with a button "Reenviar link" (below).
- **400 validation** → show `detail[].msg` under the field.

**Password rules** (NIST style, no "one capital and one number" rules):

| Rule | Message (es) |
|---|---|
| 10–128 characters | "Usa al menos 10 caracteres (una frase sirve)" |
| not a common password | "Esa contraseña es muy común" |
| does not contain your email's name | "No uses tu correo dentro de la contraseña" |

Show the hint "Tip: una frase larga es más segura que una palabra rara" under the field.

After signup, call `POST /owners/{id}/consent` once the user is logged in (Perfil brief).

## Verification link

The link in the email returns to `AUTH_EMAIL_REDIRECT_URL` (the web app, e.g. `https://<vercel-app>/verificado`). supabase-js picks the session up from the URL by itself. Show "¡Cuenta activada!" and continue to the app.

**Resend:** `POST /auth/resend-verification` with `{"email"}` → always 200, `"If the account exists and is not verified, we sent a new link."`
- Show "Si la cuenta existe, te enviamos un nuevo link".
- Limit: 3 a minute.
- Disable the button for 60 s after each tap.

## Login: `POST /auth/login`

```json
{"email": "camila@gmail.com", "password": "mi perro se llama luna"}
```

| Status | Meaning | UI (es) |
|---|---|---|
| 200 | session: `{access_token, refresh_token, expires_in, user}` | `await supabase.auth.setSession({access_token, refresh_token})`, then into the app |
| 401 | wrong email or password (same message for both) | "Correo o contraseña incorrectos." |
| 403 | right password, email not verified yet | "Primero confirma tu correo." + button "Reenviar link" |
| 429 | 5 failed attempts in 15 min (also `Retry-After` in seconds) | "Demasiados intentos. Intenta de nuevo en 15 minutos." Disable the form for `Retry-After` |

After `setSession`, supabase-js refreshes the token by itself before it expires. If the app ever holds a stale refresh token, `POST /auth/refresh` with `{"refreshToken"}` answers a new session, or 401 "Invalid or expired session." (send the user to login).

## Google

Apple stays **hidden** (it needs a paid Apple developer account).

1. `supabase.auth.signInWithOAuth({ provider: "google", options: { redirectTo: window.location.origin } })`. Store the role picked on the screen in `localStorage["cantrack.pendingRole"]` before redirecting, as today.
2. Back in the app with a session: if `session.user.app_metadata.role` is empty, call `POST /auth/role` with `{"role": pendingRole}` once.
   - 200 → `supabase.auth.refreshSession()` so the token carries the role.
   - 409 → it already had one; ignore.
3. Never call `supabase.auth.updateUser({ data: { role } })`. The API ignores `user_metadata`.

## Read the role

Read it from `session.user.app_metadata.role` (`"owner"` / `"walker"`), never from `user_metadata`.

In Spanish the walker is **"paseador"**, never "cuidador".

## Change password (Perfil → Seguridad): `POST /auth/change-password`

Bearer token, body `{"currentPassword", "newPassword"}`.

| Status | UI (es) |
|---|---|
| 200 | `{message, session}`: Supabase closes EVERY session on a password change, this one included, so call `supabase.auth.setSession(session)` with the returned tokens right away. Show "Contraseña cambiada. Cerramos tu sesión en los otros dispositivos." |
| 401 | "La contraseña actual no es correcta." (it counts toward the 5-attempt lockout) |
| 400 | the rule that failed, or "Usa una contraseña distinta a la actual" |

## Sign out: `POST /auth/logout`

Bearer token.

| Body | What it does | Button (es) |
|---|---|---|
| `{}` | this device | "Cerrar sesión" |
| `{"scope": "global"}` | every device | "Cerrar sesión en todos los dispositivos" |

Answers 204. Then call `supabase.auth.signOut({ scope: "local" })` and go to the welcome screen.

## Forgot password

Unchanged:
- `POST /auth/reset-password` with `{"email"}` always answers the same message.
- The emailed link opens the app with a recovery session.
- Then call `supabase.auth.updateUser({ password })`. This is the one place `updateUser` is fine: it changes the password, not the role.

## Errors (every endpoint)

| Status | Body | UI (es) |
|---|---|---|
| 400 | `{"detail": [{"loc", "msg", "type"}]}` | message under the field |
| 429 | `{"detail": "..."}` + `Retry-After` | wait that many seconds |
| 5xx | `{"detail": "Something went wrong on our side.", "errorId"}` | "Algo falló de nuestro lado. Código: {errorId}" |

## Never

- Read or write Supabase tables from the browser. Every table is deny-all.
- Keep tokens anywhere other than supabase-js's own storage.
