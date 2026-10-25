# Frontend brief: Inicio (owner home)

Replaces `WEATHER_SAMPLE`, `OWNER_WALKS` and the hardcoded ticket in
`apps/web/src/home/sampleData.ts` / `HomeDashboard.tsx`. Two calls, made in parallel, and nothing else.

## 1. `GET /owners/{ownerId}/home` (one call for the whole panel)

`ownerId` is `session.user.id`. Any other id answers 403.

```ts
type HomeWalk = {
  id: string; dogName: string | null; walkerName: string | null;
  requestedTime: string; durationHours: number; priceCop: number;
};
type HomePanel = {
  greetingName: string;                       // "Camila", or the email prefix until the profile has a name
  activeWalk: null | {
    id: string; dogName: string | null; walkerName: string | null;
    startedAt: string | null; minutesElapsed: number;
    lastPosition: null | { lat: number; lng: number; atEpoch: number };
  };
  nextWalk: HomeWalk | null;                  // the ticket card
  upcoming: HomeWalk[];                       // "Paseos programados", max 5, soonest first
  openRequests: { id: string; dogName: string | null; requestedTime: string; priceCop: number;
                  offerCount: number; deadlineAt: string }[];   // max 5
  counts: { dogs: number; walksDone: number; openRequests: number };
};
```

Example:

```json
{
  "greetingName": "Camila",
  "activeWalk": null,
  "nextWalk": {"id": "9b1…", "dogName": "Luna", "walkerName": "Mateo",
               "requestedTime": "2026-10-25T22:00:00+00:00", "durationHours": 0.75, "priceCop": 15000},
  "upcoming": [ "…same shape…" ],
  "openRequests": [{"id": "c3…", "dogName": "Max", "requestedTime": "2026-10-26T13:00:00+00:00",
                    "priceCop": 12000, "offerCount": 3, "deadlineAt": "2026-10-25T20:10:00+00:00"}],
  "counts": {"dogs": 2, "walksDone": 7, "openRequests": 1}
}
```

**What to draw:**
- **Greeting:** `Hola, {greetingName}`.
- **`activeWalk` present:** the walk is happening now. Show the live card ("Luna está paseando con Mateo · {minutesElapsed} min") and link it to Mis paseos → detail (live map). Poll `/home` every **20 s** while it is present, and stop when it is null.
- **`activeWalk` null and `nextWalk` present:** the ticket ("Próximo paseo · Luna · Hoy, 5:00 pm · con Mateo").
- **`walkerName` null:** write "Paseador asignado".
- **`openRequests`:** chips "3 ofertas" that link to Explorar with that request open.
- **Counts:** the small stats row.

**Dates:** every time is UTC ISO. Format with `Intl.DateTimeFormat("es-CO", { timeZone: "America/Bogota" })`.

## 2. `GET /weather?lat=&lng=` (the weather card)

Use the owner's saved home point (Perfil, `home.lat/lng`), falling back to the browser location, then to Pasto (1.2136, -77.2811).

```ts
type Condition = "clear" | "cloudy" | "fog" | "drizzle" | "rain" | "snow" | "storm";
type Weather = {
  lat: number; lng: number; temperatureC: number; condition: Condition;
  hours: { at: string; temperatureC: number; condition: Condition; precipitationProbability: number }[]; // 12
  bestWindow: null | { start: string; end: string; reason: "dry_and_mild" };
  stale: boolean;
};
```

| condition | es | icon |
|---|---|---|
| clear | Despejado | sun (moon after 18:00) |
| cloudy | Nublado | cloud |
| fog | Niebla | cloud |
| drizzle | Llovizna | cloud-rain |
| rain | Lluvia | cloud-rain |
| snow | Nieve | snow |
| storm | Tormenta | bolt |

- **`bestWindow`:** a suggestion chip, "Mejor hora para pasear: 11:00–13:00 (seco y templado)". Tapping it opens Explorar with that start time prefilled. When it is null, show "Hoy no hay una ventana seca; revisa el pronóstico".
- **`stale: true`:** keep the card but add a small "Actualizado hace un rato".
- **Cache:** the API caches for 15 minutes, so never poll it. Refetch only on focus, at most every 15 minutes.

## States and errors

| Case | UI |
|---|---|
| loading | the running-dog loader (`DogLoader`) inside each card, not the whole page |
| `/home` 403 | log out (the session belongs to someone else) |
| `/home` 5xx | "Algo falló de nuestro lado. Código: {errorId}" + retry button |
| `/weather` 503 | hide the weather card, not an error banner |
| 429 | wait `Retry-After` seconds before retrying |
| offline | keep the last panel, add the banner "Sin conexión" |

## Never

- Fetch dogs, walks or offers one by one to build this screen. `/home` already has everything.
- Call Open-Meteo or any AI from the browser. The architecture test fails if the web app calls the AI.
