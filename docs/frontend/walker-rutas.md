# Frontend brief: Rutas (walker's day route)

Replaces `WalkerRouteScreen.tsx`'s legacy `/routes` list for the main view: the route is now **built by the
server from the walks he won**, no manual route creation. `walkerId` is `session.user.id`.

## `GET /walkers/{walkerId}/route?date=YYYY-MM-DD`

No `date` = today in Bogota. Any other format (`19/11/2026`, `tomorrow`) answers 400.

```ts
type RouteStop = {
  requestId: string; dogName: string | null;
  status: "assigned" | "in_walk" | "done";
  requestedTime: string; pickupLat: number; pickupLng: number;
  legKm: number;          // straight-line km from the previous stop (or from him, for the first one)
  legMinutes: number;     // walking minutes for that leg
  legSource: "graph" | "straight" | "start";
};
type DayRoute = {
  date: string;                          // the Bogota day asked for
  start: { lat: number; lng: number } | null;   // where he is (null = offline)
  stops: RouteStop[];                    // in pickup order
  totalKm: number; totalMinutes: number;
  nextStopId: string | null;             // first stop not done yet
};
```

```json
{"date": "2026-10-25", "start": {"lat": 1.214, "lng": -77.2811},
 "stops": [
  {"requestId": "a1…", "dogName": "Luna", "status": "done", "requestedTime": "2026-10-25T14:00:00+00:00",
   "pickupLat": 1.2136, "pickupLng": -77.2811, "legKm": 0.04, "legMinutes": 1, "legSource": "straight"},
  {"requestId": "b2…", "dogName": "Max", "status": "assigned", "requestedTime": "2026-10-25T16:00:00+00:00",
   "pickupLat": 1.2226, "pickupLng": -77.2811, "legKm": 1.0, "legMinutes": 9, "legSource": "graph"}],
 "totalKm": 1.04, "totalMinutes": 10, "nextStopId": "b2…"}
```

**What to draw:**
- A vertical timeline: one row per stop, "9:00 am · Luna", with a ✓ when `done` and a pulsing dot on
  `nextStopId`.
- Between rows: "1,0 km · 9 min". When `legSource` is `"straight"`, add "(aprox.)": the street graph did
  not connect those two points, so the minutes are a straight-line estimate at 4,5 km/h.
- A map with the pins in order and `start` as "Tú". `start` null: no "Tú" pin and the first leg shows "—".
- Header: "Hoy · 3 paseos · 2,1 km · 25 min". A day picker (‹ ›) changes `date`.
- Tapping a stop opens the walk detail (`GET /bidding/requests/{id}`, already documented in `mis-paseos.md`)
  where check-in / check-out live.

**Polling:** refetch on focus and every **60 s** while the screen is open (stops change status as he checks
in and out).

**Why it matters for the professor:** the order comes from the hand-written AVL tree (by pickup time) and the
minutes from Dijkstra over the street graph. Say so in the empty-state help text if you want, but never
reorder the stops in the client.

| Case | UI |
|---|---|
| `stops` empty | "No tienes paseos este día. Mira Solicitudes" |
| 400 | reset the day picker to today |
| 429 (30 a minute) | keep the last route |
