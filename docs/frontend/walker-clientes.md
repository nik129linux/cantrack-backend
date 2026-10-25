# Frontend brief: Clientes (walker)

Replaces `WalkerClientsScreen.tsx`'s call to the legacy `GET /clients`. A client is an owner he has won at
least one walk from (assigned, in progress or done). `walkerId` is `session.user.id`.

## 1. `GET /walkers/{walkerId}/clients`

```ts
type Client = {
  ownerId: string; ownerName: string | null;
  dogs: { id: string; name: string | null }[];
  walksDone: number; upcoming: number;
  lastWalkAt: string | null;      // null = only upcoming walks so far
  grossCop: number;               // what he earned with this client, before the simulated commission
};
```

Ordered by the most recent walk. Row: avatar initials, "Camila · Luna, Max", "7 paseos · último hace 3 días",
"$105.000". `ownerName` null = "Dueño". Empty: "Cuando ganes tu primer paseo, tus clientes aparecen aquí".

## 2. `GET /walkers/{walkerId}/clients/{ownerId}` (detail)

Everything above plus:

```ts
type ClientDetail = Client & {
  phone: string | null;     // ONLY while a walk with this owner is assigned or in progress
  dogs: { id: string; name: string | null; profile: DogProfile | null }[];   // care notes
  walks: { id: string; dogName: string | null; status: "assigned" | "in_walk" | "done";
           requestedTime: string; priceCop: number }[];                     // newest 10
};
```

- **Care notes first:** for each dog, the `profile` card (size, temperament, notes). This is what he reads
  before the pickup.
- **Phone:** show a "Llamar" button only when `phone` is not null. When null, write "El contacto se
  habilita cuando tengas un paseo asignado" (InDrive rule: contact only during the trip).

## Errors

| Status | UI |
|---|---|
| 404 `Not one of your clients.` | back to the list |
| 401 | login |
| 429 (30 a minute) | "Espera un momento" |

**Don't:** cache the phone after the walk ends; read dog data from Supabase directly.
