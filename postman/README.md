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
