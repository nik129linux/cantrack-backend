# Frontend brief: Perfil (walker)

Replaces `WalkerProfileScreen.tsx`'s `PUT /walker-profile` (kept for old clients, do not use it in new code).
Same contract as the owner's Perfil (`perfil.md`). `walkerId` is `session.user.id`.

## 1. Profile: `GET` / `PUT /walkers/{walkerId}/profile`

```ts
type WalkerProfile = {
  walkerId: string; email: string;
  displayName: string | null; bio: string | null; serviceArea: string | null;
  pricePerWalk: number | null; phone: string | null;
  avatarUrl: string | null;              // signed URL, valid 1 hour: never store it
  consent: { version: string; at: string } | null;
  stats: { walksDone: number; rating: { average: number | null; count: number } };
};
```

`PUT` sends the whole form. **`displayName` (1-80) and `pricePerWalk` (0-1.000.000) are required**;
`bio` (≤500), `serviceArea` (≤120) and `phone` (`+57 300 123 4567` style) may be `null`. Any violation is 400:
validate in the form with the same limits. A brand-new walker gets `displayName: null`: open the form
straight away ("Completa tu perfil para recibir solicitudes").

## 2. Avatar: `POST /walkers/{walkerId}/avatar` (multipart field `photo`)

≤ 2 MB (413 `The avatar must be 2 MB or less.`), any normal image (400 `Unreadable image.`). The server
re-encodes it to a 512 px WEBP. **409 `Save your profile before adding a photo.`** until the first `PUT`.

## 3. Consent (Ley 1581): `POST /walkers/{walkerId}/consent {"version": "2026-10"}`

Call it once, right after the first profile save, when he ticks "Acepto la política de tratamiento de datos".
Also 409 before the first `PUT`.

## 4. History: `GET /walkers/{walkerId}/walks?tab=upcoming|active|past`

```json
[{"id": "…", "dogName": "Luna", "ownerName": "Camila", "status": "done",
  "requestedTime": "2026-10-20T15:00:00+00:00", "durationHours": 1.0, "priceCop": 17000, "rating": 4.5}]
```

Max 50. `past` is newest first and carries the owner's `rating` (may be fractional, may be null). Money for
the period lives in Ganancias (`walker-inicio.md`, `/earnings`).

## 5. Export: `GET /walkers/{walkerId}/export` (2 a minute)

A JSON download (`Content-Disposition: attachment; filename="cantrack-export.json"`) with
`exportedAt, profile, position, offers, walks, ratingsReceived`. Walks carry his own facts only; owners'
pickup addresses are not his data and are not included. Button: "Descargar mis datos".

## 6. Delete account: `DELETE /walkers/{walkerId}` with body `{"confirm": "DELETE"}`

- Ask him to type `ELIMINAR` in a dialog and send `{"confirm": "DELETE"}` (exact string, else 400).
- **409 `Finish your walk in progress before deleting the account.`** while a dog is out with him.
- On 204: his assigned walks go back to the owners as open requests, his offers and location are erased,
  and the owners keep their history. Sign out and go to the welcome screen.

| Status | UI |
|---|---|
| 400 on PUT | inline field errors |
| 409 avatar/consent | "Guarda tu perfil primero" |
| 413 | "La foto debe pesar máximo 2 MB" |
| 429 | "Espera un momento" |

**Don't:** keep `avatarUrl` beyond the hour, call `supabase.auth.admin` from the browser, or delete with
`supabase-js` (the server is what keeps the owners' history).
