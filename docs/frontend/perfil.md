# Frontend brief: Perfil

Replaces the `ComingSoon` profile tab for owners.

## 1. `GET /owners/{ownerId}/profile`

```ts
type OwnerProfile = {
  ownerId: string; email: string;
  displayName: string | null; phone: string | null;
  avatarUrl: string | null;                       // signed, valid 1 hour: don't store it
  home: null | { lat: number; lng: number; label: string };
  consent: null | { version: string; at: string };
  stats: { dogs: number; walksDone: number };
};
```

When `displayName` is null, show the "Completa tu perfil" card first.

## 2. Edit: `PUT /owners/{ownerId}/profile`

```json
{"displayName": "Camila", "phone": "+57 300 123 4567",
 "home": {"lat": 1.2136, "lng": -77.2811, "label": "Casa, Las Cuadras"}}
```

| Field | Rule | Error copy |
|---|---|---|
| displayName | 1–80 characters | "Escribe tu nombre (máx. 80)" |
| phone | optional, digits and spaces, optional `+`, 7–20 | "Teléfono no válido" |
| home | optional; label ≤ 120 | "Ubicación no válida" |

`home` is the default pickup point in Explorar and the weather point in Inicio.

## 3. Photo: `POST /owners/{ownerId}/avatar` (multipart, field `photo`)

- **Accepted files:** PNG, JPEG or WebP up to 2 MB. The server re-encodes the photo to 512 px WebP.
- **Before uploading,** resize on the client to max 1024 px; that keeps it well under 2 MB.
- **413** "The avatar must be 2 MB or less." → "La foto pesa más de 2 MB".
- **400** "Unreadable image." → "No pudimos leer esa imagen".
- **Response:** `{avatarUrl}`.

## 4. Data protection (Ley 1581 de 2012)

**Consent:** right after signup, when the "Acepto el tratamiento de datos…" box was checked, call `POST /owners/{ownerId}/consent` with `{"version": "2026-10"}` → `{version, at}`. Show it in the profile: "Aceptaste la política de datos (2026-10) el 25 oct 2026".

**Download my data:** `GET /owners/{ownerId}/export` → JSON file (`cantrack-export.json`).
- Button "Descargar mis datos".
- Save the file with the Content-Disposition name.
- Limit: twice a minute.

**Delete my account:** `DELETE /owners/{ownerId}` with body `{"confirm": "DELETE"}`.
- Use a two-step dialog. The owner types **ELIMINAR**; the app then sends the literal string `"DELETE"`.
- 204 → sign out and go to the welcome screen.
- 409 "Finish your walk in progress before deleting the account." → "Tienes un paseo en curso. Podrás eliminar la cuenta cuando termine."
- What it does: cancels open/assigned walks, deletes dogs, profile and photo, and the login.

## Errors

| Status | UI |
|---|---|
| 403 | the session belongs to someone else: sign out |
| 429 | wait `Retry-After` |
| 5xx | "Algo falló de nuestro lado. Código: {errorId}" |
