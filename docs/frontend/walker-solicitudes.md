# Frontend brief: Solicitudes (walker inbox, InDrive style)

Replaces `WalkerInboxScreen.tsx`'s calls to the **deprecated** `/requests` routes and `WALKER_REQUESTS`.
The owner names a fare; the paseador **accepts it in one tap or counter-offers**; the owner picks one offer.
`walkerId` is `session.user.id`. The walker must be **online** (see `walker-inicio.md`) or the inbox is empty.

## 1. `GET /bidding/requests/open` (the inbox, poll every 10 s while the screen is open)

```ts
type RequestCard = {
  id: string; status: "open"; dogId: string; dogName: string | null; ownerName: string | null;
  requestedTime: string; windowEnd: string; durationHours: number; deadlineAt: string;
  fareCop: number;            // the owner's fare (same as priceCop)
  priceCop: number;
  pickupLat: number; pickupLng: number;
  distanceKm: number;         // from the walker, 1 decimal
  myOffer: null | { id: string; priceCop: number; kind: "accept" | "counter" };
};
```

Only requests he may take come back (inside 2 km, deadline not passed, and either a free schedule or a
compatible pack on the route he is already walking). Offline = `[]`.

**Card:** "Luna · 45 min · a 0,8 km · Hoy 5:00 pm", the fare big ("$20.000"), owner first name, and a
countdown to `deadlineAt`. When `myOffer` exists, replace the buttons with "Oferta enviada · $22.000" and a
"Retirar" link.

## 2. Answer a request: `POST /bidding/requests/{id}/offers`

```ts
// one tap: take the owner's fare
{"kind": "accept", "etaMin": 8}
// counter-offer: steps of $1.000, at least $8.000, at most 2x the fare
{"kind": "counter", "priceCop": 22000, "etaMin": 8}
// -> 201 {"id": "…", "score": 0.83, "priceCop": 22000, "kind": "counter"}
```

`etaMin` = minutes to the pickup: `distanceKm / 4.5 * 60`, rounded. Counter UI: the fare with `−` / `+`
buttons of $1.000, clamped to `[max(8000, …), 2 × fare]`. A counter equal to the fare comes back as
`kind: "accept"`; that is expected.

## 3. Withdraw: `DELETE /bidding/requests/{id}/offers/mine` → 204

Only while the request is still open. He can send a new offer afterwards.

## 4. `GET /walkers/{walkerId}/offers?status=pending|won|lost` (tab "Mis ofertas")

No `status` = all, newest first, max 50.

```json
[{"offerId": "…", "requestId": "…", "dogName": "Luna", "requestedTime": "2026-10-25T22:00:00+00:00",
  "durationHours": 0.75, "fareCop": 20000, "priceCop": 22000, "kind": "counter",
  "status": "won", "requestStatus": "assigned"}]
```

Chips: `pending` "Esperando al dueño", `won` "¡Ganaste!" (link to Rutas), `lost` "No elegida".

## Errors

| Status / detail | UI |
|---|---|
| 409 `Go online to send offers.` | "Conéctate para ofertar" + the switch |
| 409 `Walker already offered on this walk.` | refresh the card (it now has `myOffer`) |
| 403 `Walk not visible to you.` | "Esta solicitud ya no está disponible", drop the card |
| 400 (counter price) | "La contraoferta debe ser de $8.000 a {2× tarifa}, en pasos de $1.000" |
| 404 `You have no offer on this request.` | refresh |
| 409 `The request is no longer open.` | "El dueño ya eligió", refresh |
| 429 | "Espera un momento" |

**Don't:** call the legacy `/requests/{id}/accept|decline` (deprecated), compute eligibility in the client,
or send a price with `kind: "accept"` (400).
