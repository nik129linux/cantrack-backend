# Frontend brief: Inicio (walker home)

Replaces the walker side of `HomeDashboard.tsx` (the "Próximo recorrido" ticket, `WALKER_REQUESTS`) and the
"Hola, cuidador" greeting. **The role is "paseador" in every string, never "cuidador".**
`walkerId` is `session.user.id` in every path below. Any other id answers 403.

## 1. The online switch (InDrive style)

The walker only receives requests while **online**. Online means the app sent its position in the last
**120 s**. There is no flag on the server: the stored position IS the switch.

```ts
// switch ON: send the current position (navigator.geolocation)
PUT /walkers/{walkerId}/availability   {"online": true, "lat": 1.2140, "lng": -77.2811}
// -> 200 {"online": true, "updatedAt": "2026-10-25T14:00:00+00:00"}

// switch OFF: the server DELETES the stored location (Ley 1581: we keep no location of an offline walker)
PUT /walkers/{walkerId}/availability   {"online": false}
// -> 200 {"online": false, "updatedAt": null}
```

While the switch is on, keep the heartbeat alive with `PUT /walker-position {"lat", "lng"}` every **30 s**
(that endpoint allows 4 calls a minute). If the tab sleeps for more than 2 minutes the walker is offline
on its own; re-read `online` from `/home` when the tab becomes visible again.

`online` must be a real boolean: `"yes"`, `1` and `"true"` answer 400. The switch allows 10 changes a minute (429 after).

## 2. `GET /walkers/{walkerId}/home` (one call for the whole panel)

```ts
type WalkerHomeWalk = {
  id: string; dogName: string | null; ownerName: string | null;
  requestedTime: string; durationHours: number; priceCop: number;   // priceCop = the price HE agreed
};
type WalkerHome = {
  greetingName: string;            // profile name, or the email prefix until he saves a profile
  online: boolean;
  activeWalk: WalkerHomeWalk | null;   // in progress now
  nextWalk: WalkerHomeWalk | null;     // soonest assigned walk: the ticket
  today: { walks: number; grossCop: number; commissionCop: number; netCop: number };
  rating: { average: number | null; count: number };
  openNearby: number;              // open requests within 2 km; 0 while offline
  pendingOffers: number;           // his offers still waiting for the owner
};
```

```json
{
  "greetingName": "Mateo", "online": true, "activeWalk": null,
  "nextWalk": {"id": "9b1…", "dogName": "Luna", "ownerName": "Camila",
               "requestedTime": "2026-10-25T22:00:00+00:00", "durationHours": 0.75, "priceCop": 17000},
  "today": {"walks": 2, "grossCop": 35000, "commissionCop": 3500, "netCop": 31500},
  "rating": {"average": 4.67, "count": 12}, "openNearby": 3, "pendingOffers": 1
}
```

**What to draw:**
- `Hola, {greetingName}` and the big switch: "Conectado" / "Desconectado".
- **Offline:** grey the panel, "Conéctate para recibir solicitudes". Hide `openNearby`.
- **`activeWalk`:** live card "Paseando a Luna · desde 5:00 pm". Poll `/home` every **20 s** while it exists.
- **`nextWalk`:** the ticket "Próximo recorrido · Luna · Hoy 5:00 pm · $17.000".
- **`ownerName` null:** write "Dueño".
- **Today:** "Hoy: 2 paseos · $31.500 netos". Under it, small: "Comisión simulada 10%: $3.500".
- **`openNearby > 0`:** chip "3 solicitudes cerca" that opens Solicitudes.
- **Rating:** "★ 4.7 (12)"; `average` null = "Sin calificaciones aún".

## 3. `GET /walkers/{walkerId}/earnings?period=day|week|month` (Ganancias)

Default `week` (Monday to Sunday, Bogota days). **The commission is simulated**: there are no payments in
CanTrack. Always show the word "simulada" next to it (`simulated` is always `true`).

```json
{"period": "week", "from": "2026-10-19", "to": "2026-10-25", "simulated": true, "commissionPct": 10,
 "walks": 6, "grossCop": 102000, "commissionCop": 10200, "netCop": 91800,
 "byDay": [{"date": "2026-10-19", "walks": 1, "grossCop": 15000, "netCop": 13500}, "…one per day, zeros included…"]}
```

Draw `byDay` as a bar per day (7 bars for a week, 28-31 for a month). `from`/`to` are already local dates:
do not convert them.

## States and errors

| Case | UI |
|---|---|
| loading | skeleton cards, the switch disabled |
| `today.walks == 0` | "Aún no hay paseos hoy" |
| network error | keep the last panel, banner "Sin conexión" |
| 401 | back to login |
| 403 | never happens with `session.user.id`; log it |
| 429 on the switch | "Espera un momento" and re-enable after 10 s |

**Dates:** every time is UTC ISO; format with `Intl.DateTimeFormat("es-CO", { timeZone: "America/Bogota" })`.
**Money:** `Intl.NumberFormat("es-CO", { style: "currency", currency: "COP", maximumFractionDigits: 0 })`.

**Don't:** call Supabase tables from the browser, compute the commission in the client, or keep sending the
position after the switch is off.
