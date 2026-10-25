# Frontend brief: Explorar (inDrive-style request)

Replaces `RequestPanel` (sample fare stepper) and moves `OwnerDiscoverScreen` off the legacy
`/requests` flow (now marked deprecated in OpenAPI) onto `/bidding`.

## Flow

```
form (dog, duration, start, fare, pickup)  ──POST /bidding/requests──▶  waiting for offers
      ▲  optional: "Escríbelo y lo lleno yo"                                   │ poll offers every 4 s (ETag)
      │  POST /bidding/requests/parse                                          ▼
      │                                                              ranked offer cards
      └────────── raise the fare and post again ◀── no offers by deadline   │ POST …/pick
                                                                             ▼
                                                                   assigned → Mis paseos
```

## 1. Form

**Fare suggestion:** `GET /bidding/fare-suggestion?durationMinutes=45&lat=&lng=`

```json
{"suggestedCop": 15000, "basis": "market", "sampleSize": 12, "minCop": 8000, "stepCop": 1000}
```

- Start the fare stepper at `suggestedCop`. Steps of `stepCop`, minimum `minCop`, maximum 1,000,000.
- When `basis` is `"market"`, show "Precio típico en tu zona"; when it is `"default"`, show "Precio sugerido".
- Refetch only when the duration or the pickup changes.

**Durations:** `30 | 45 | 60 | 90 | 120` minutes. Anything else is a 400.

**Nearby walkers:** `GET /bidding/nearby-walkers?lat=&lng=` → `{"count": 4, "radiusKm": 2}`.
- Show "4 cuidadores cerca".
- When it is 0, show "Ahora no hay cuidadores cerca; tu solicitud igual quedará publicada".
- The answer is only a count, so never draw walkers on the map from it.

**Create:** `POST /bidding/requests`

```json
{"dogId": "…", "windowStart": "2026-10-26T22:00:00+00:00", "durationMinutes": 45,
 "priceCop": 15000, "pickupLat": 1.2136, "pickupLng": -77.2811}
```

- **`windowStart`** must have a timezone offset, be in the future and fall within the next 30 days.
- **Limit:** 10 requests a minute.
- **Response 201:** `{id, status: "open", dogId, requestedTime, windowEnd, durationHours, priceCop, pickupLat, pickupLng, deadlineAt}`.

## 2. Optional assistant: "Escríbelo y lo lleno yo"

`POST /bidding/requests/parse` with `{"text": "Luna mañana a las 5pm, 45 min, 15 mil"}` (1–280 characters).

```json
{"draft": {"dogId": "…", "windowStart": "2026-10-26T17:00:00-05:00", "durationMinutes": 45, "priceCop": 15000},
 "missing": [], "aiStatus": "ok"}
```

- **It never creates anything.** Fill the form with `draft`, highlight the fields in `missing` ("Completa: hora"), and the owner still taps "Publicar".
- **`aiStatus: "unavailable"`:** toast "El asistente no respondió; llena el formulario".
- **503:** hide the assistant button for the session.
- **429** "Daily assistant limit reached." → "Ya usaste el asistente 10 veces hoy".

## 3. Offers (polling)

`GET /bidding/requests/{id}/offers`, every **4 s** while the request is `open`:
- send `If-None-Match` with the last `ETag`;
- **304** = nothing changed, keep the list.

```ts
type Offer = {
  offerId: string; walkerId: string; walkerName: string | null;
  priceCop: number; etaMin: number; distanceKm: number;
  score: number; scoreParts: { price: number; distance: number; rating: number }; // each 0..1
  recentRating: number | null; allTimeRating: number | null; ratingCount: number;
};
```

**Card:**
- "Mateo · ★ 4.8 (23) · a 0.6 km · llega en 10 min · $14.000".
- The first card gets "Mejor opción" plus one line from its strongest `scoreParts` key: price → "el mejor precio", distance → "el más cerca", rating → "el mejor calificado".
- **No rating** (`ratingCount` 0): show "Nuevo".

**Pick:** `POST /bidding/requests/{id}/pick` with `{"offerId"}`.
- 200 → go to Mis paseos.
- 409 → "Otro cuidador ya fue asignado o la solicitud cambió", then refetch.

**Deadline:** after `deadlineAt` with no pick, the best offer is auto-picked. With no offers the request expires: offer "Subir $2.000 y volver a publicar".

## 4. Your requests list

`GET /owners/{ownerId}/requests?status=open&limit=20&cursor=` → `{items, nextCursor}`.
- Paginate with `nextCursor` until it is null.
- Item: `{id, status, dogId, dogName, requestedTime, durationHours, priceCop, offerCount, deadlineAt, walkerId}`.

## Errors

| Status | When | UI |
|---|---|---|
| 400 | invalid field (`detail[].loc`) | message under the field |
| 403 | not your request / wrong role | "No tienes acceso a esta solicitud" |
| 409 | state changed meanwhile | refetch + toast "La solicitud cambió" |
| 429 | rate limit | disable the button for `Retry-After` s |
| 5xx | `{detail, errorId}` | "Algo falló de nuestro lado. Código: {errorId}" |

## Never

- Use the legacy `/requests` endpoints for new screens.
- Call the AI from the browser. The assistant goes through `/bidding/requests/parse`.
- Poll offers without `If-None-Match`.
