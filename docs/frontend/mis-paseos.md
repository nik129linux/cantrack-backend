# Frontend brief: Mis paseos

Replaces the "Paseos" tab (`OwnerActivityScreen`) for bidding walks.

## 1. Tabs: `GET /owners/{ownerId}/walks?tab=upcoming|active|past&limit=20&cursor=`

| tab | contains | order |
|---|---|---|
| `upcoming` | assigned walks | soonest first |
| `active` | the walk happening now | |
| `past` | done, cancelled, expired | newest first |

```ts
type WalkItem = {
  id: string; status: "assigned" | "in_walk" | "done" | "cancelled" | "expired";
  dogId: string; dogName: string | null; walkerId: string | null; walkerName: string | null;
  requestedTime: string; durationHours: number; priceCop: number;   // the agreed price
  canRate: boolean; hasReport: boolean;
};
// response: { items: WalkItem[]; nextCursor: string | null }
```

- **Badges:** `canRate` → "Califica a Mateo"; `hasReport` → a "Reporte" chip.
- **Status labels:** assigned "Programado", in_walk "En curso", done "Terminado", cancelled "Cancelado", expired "Sin paseador".

## 2. Detail: `GET /bidding/requests/{id}`

```ts
type WalkDetail = {
  id: string; status: string;
  dog: { id: string; name: string };
  walker: null | { id: string; name: string | null; allTimeRating: number | null; ratingCount: number };
  requestedTime: string; windowEnd: string; durationHours: number; priceCop: number; offerCount: number;
  timeline: { kind: "check_in" | "check_out" | string; at: string }[];
  rating: number | null;
  report: null | { status: "ok" | "unavailable" | "disabled"; text: string | null;
                   distanceKm: number; durationMin: number };
  allowedActions: ("pick" | "cancel" | "no_show" | "track" | "replay" | "repeat" | "rate"
                   | "check_in" | "check_out" | "send_gps")[];
};
```

**Draw buttons ONLY from `allowedActions`.** That way the UI never offers an action that would answer 409.

| action | button (es) | call |
|---|---|---|
| cancel | Cancelar paseo | `POST /bidding/requests/{id}/owner-cancel` |
| no_show | El paseador no llegó | `POST /bidding/requests/{id}/no-show` (409 = the walker is at the door: "Mateo está en el punto de recogida; llámalo") |
| track | Ver en vivo | live map below |
| rate | Calificar | `POST /bidding/requests/{id}/ratings` `{rating: 1..5}` |
| replay | Ver recorrido | `GET /bidding/requests/{id}/replay` |
| repeat | Repetir paseo | `POST /bidding/requests/{id}/repeat` `{windowStart}` → new open request, go to Explorar |

**Report card:** "2.22 km · 47 min" plus `text` when `status` is "ok". With "unavailable"/"disabled", show only the numbers. The report is generated once at check-out, so reading it costs nothing.

## 3. Live map: `GET /bidding/requests/{id}/live` (only while `track` is allowed)

`{"position": {"lat": 1.23, "lng": -77.29, "atEpoch": 1791195000} | null, "minutesElapsed": 12}`

- Poll every **10 s**.
- **409** "This walk is not in progress." → stop polling and refetch the detail (the walk ended).
- `position` null → "Esperando la primera ubicación del paseador".
- **Map tiles:** use Leaflet with OpenStreetMap.

## 4. Walker side (for the walker app, same endpoints)

`POST /bidding/requests/{id}/gps` with `{"points": [{"lat", "lng", "atEpoch"}]}`:
- 1 to 20 points per call;
- in increasing time order, after the last point sent;
- at most 6 calls a minute, so batch every 10–15 s.

| Answer | Meaning |
|---|---|
| 400 | out-of-order or future points |
| 409 | the walk is not in progress |

## Errors

| Status | UI |
|---|---|
| 404 "Walk not found." | "Este paseo no existe o no es tuyo" + back |
| 409 | the state changed: refetch the detail |
| 429 | wait `Retry-After` |
| 5xx | "Algo falló de nuestro lado. Código: {errorId}" |
