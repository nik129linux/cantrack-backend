# CanTrack API — Postman / Newman

The professor tests the running app with Postman. This folder holds a Postman v2.1
collection for the walk-bidding API (FR-23..FR-27) and an environment file.

- `cantrack-bidding.postman_collection.json` — 15 requests: the happy path (post a
  request → two offers → ranked list → pick → check-in → check-out → rate → replay),
  followed by the negative cases (second pick → 409, check-out from `assigned` → 409,
  wrong-owner pick → 403, second offer by the same walker → 409, rating above 5 → 400).
  Every request carries a `pm.test` assertion.
- `cantrack-bidding.postman_environment.json` — `baseUrl` plus token/id variables. The
  tokens are placeholders; it never contains a real secret.

## Import into Postman

File → Import → select both files, then pick "CanTrack Bidding (local)" as the active
environment.

## Setup (both Postman and Newman)

1. Start the API locally: from `apps/api-py/`, `uv run uvicorn cantrack_api.main:app --reload`.
2. Create one owner and two walker accounts (Supabase Auth) and copy each access token.
3. Fill the environment: `ownerToken`, `walkerToken`, `walker2Token`, and `dogId`
   (a dog the owner owns). Leave `walkId`/`offerId`/`offer2Id` empty — the happy path
   stores them with `pm.environment.set` as it runs.

The requests run top to bottom and depend on that order (the happy path assigns and
finishes the walk that the negative cases then probe).

## Run from the command line with Newman

```
npx newman run cantrack-bidding.postman_collection.json \
  -e cantrack-bidding.postman_environment.json
```

Keep real tokens out of git: copy the environment file to an untracked one
(`cp cantrack-bidding.postman_environment.json local.env.json`), fill the tokens there,
and run Newman with `-e local.env.json`.

## Security exploits collection (PR0)

`cantrack-security.postman_collection.json` + `cantrack-security.postman_environment.json`:
20 attacks a logged-in user can try with their own token (role spoofing, IDOR, overflow,
oversized bodies, error leaks, account enumeration, login flood). Run `Setup` first, then
`Exploits`; every request must get the 4xx in its name. The full list, with the regression
test behind each one, is in `docs/security/exploits-security.md`.

```
npx newman run cantrack-security.postman_collection.json -e local.security.env.json
```

Against the deployed API set `isProduction=true` (Swagger must then be a 404).

## Walker/owner endpoints collection (walker glass)

`cantrack-walker-glass.postman_collection.json` covers the eight new walker/owner
endpoints: `POST /bidding/requests/{id}/raise`, `PUT /owners/{ownerId}/favorites/{walkerId}`,
`GET /owners/{ownerId}/walks/summary`, `GET`/`PUT /walkers/{walkerId}/availability`,
`PUT /walkers/{walkerId}/clients/{ownerId}/dogs/{dogId}/note`, `PUT /walkers/{walkerId}/goal`
and `GET /walkers/{walkerId}/walking-route`. Run top to bottom in three folders:

- **Happy path** — reads the walker's availability, earnings and client-detail baselines,
  then builds the data the rest needs: the walker goes online, the owner posts two open
  requests (one is raised 20000 → 22000, the other is accepted by the walker and picked,
  which is what makes the walker a client and a favourite candidate), then favourites,
  dog note, weekly goal and walking route are exercised for real.
- **Exploits** — one no-token 401, one wrong-account 403/404 and one bad-body 400 per
  endpoint (where the API defines one); every name carries the expected 4xx and the
  description says which rule is behind it. `PUT /owners/{ownerId}/favorites/{walkerId}`
  has no happy path for a stranger, so its exploit asserts the documented 404.
- **Cleanup** — unfavourites the walker, puts the note back to its captured baseline,
  cancels both walk requests, and restores availability and the weekly goal to the
  state captured at the start (the API cannot unset a goal, so a walker who had none
  keeps the 10000 minimum).

Environment: `baseUrl`, `ownerToken`, `ownerId`, `otherOwnerToken`, `otherOwnerId`,
`walkerToken`, `walkerId`, `dogId` (a dog owned by the owner) — kept in the untracked
`local.env.json` exactly like the other collections.

```
npx newman run cantrack-walker-glass.postman_collection.json -e local.env.json
```
