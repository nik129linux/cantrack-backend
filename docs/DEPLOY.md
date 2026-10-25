# Deploy the CanTrack API to Railway

Runbook for deploying `apps/api-py` (Dockerfile) as a Railway service.

## Prerequisites

- A Railway account on the free trial.
- The Railway CLI installed and logged in:

```bash
railway login
```

- The frontend already deployed on Vercel over https (you need its URL for `WEB_ORIGIN`).
- Supabase project credentials and an Ollama Cloud API key (see variables below).

## Create the project and service

```bash
railway init
railway link
```

`railway init` creates the project; name the API service `cantrack-api` when the
dashboard asks (or `railway add --service cantrack-api`). Deploys upload only
`apps/api-py` (see Deploy), so its `railway.toml` and `Dockerfile` sit at the
build root. If you later connect the GitHub repo instead of using the CLI, set
the service **Root Directory** to `apps/api-py` (Settings > Source).

Keep **one replica**: the rate limiter lives in process memory, so with two
replicas every limit would be split between them and stop protecting logins.

## Environment variables

Set these in the Railway dashboard (service > Variables) or via `railway variables set`.
Never commit real secret values to git.

| Variable | What it is for | Where to get it |
|---|---|---|
| `SUPABASE_URL` | Supabase project URL the API talks to | Supabase dashboard > Project Settings > API > Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side key for Supabase (never expose to the browser) | Supabase dashboard > Project Settings > API > service_role key |
| `WEB_ORIGIN` | The https Vercel URL of the frontend allowed by CORS (production refuses to boot without an https origin) | Vercel dashboard > your frontend project > Domains |
| `ENV=production` | Production mode: hides `/docs`, `/redoc`, `/openapi.json` | Literal value `production` |
| `CLIENT_IP_HEADER=X-Real-IP` | Trust Railway's edge-overwritten `X-Real-IP` header for anonymous rate-limit IPs | Literal value `X-Real-IP` |
| `TRUSTED_PROXY_IPS` | Leave `127.0.0.1`: forwarded headers are read via CLIENT_IP_HEADER, not uvicorn | Literal value `127.0.0.1` |
| `OLLAMA_API_KEY` | Key for Ollama Cloud vision/text calls (AI features stay disabled when unset) | Ollama Cloud dashboard > API keys |
| `OLLAMA_HOST` | Ollama base URL | `https://ollama.com` unless self-hosting |
| `OLLAMA_VISION_MODEL` | Model for photo checks (route intake, checkout evidence) | Ollama model name, e.g. `gemma4:31b` |
| `OLLAMA_TEXT_MODEL` | Model for text generation (explore AI, walk reports) | Ollama model name, e.g. `gemma4:31b` |
| `AI_WALK_REPORT` | Set to `off` to disable AI walk reports, leave unset to enable | Literal value `off` or unset |

Example (placeholders only, never real secrets):

```bash
railway variables set ENV=production
railway variables set WEB_ORIGIN=https://cantrack-web.vercel.app
railway variables set SUPABASE_URL=https://xyzcompany.supabase.co
railway variables set SUPABASE_SERVICE_ROLE_KEY=paste-from-supabase-dashboard
railway variables set CLIENT_IP_HEADER=X-Real-IP
railway variables set TRUSTED_PROXY_IPS=127.0.0.1
railway variables set OLLAMA_HOST=https://ollama.com
railway variables set OLLAMA_API_KEY=paste-from-ollama-dashboard
railway variables set OLLAMA_VISION_MODEL=gemma4:31b
railway variables set OLLAMA_TEXT_MODEL=gemma4:31b
```

## Deploy

From the repository root (uploads only the API folder, as the build root):

```bash
railway up ./apps/api-py --path-as-root --service cantrack-api
```

The first build takes a few minutes (it bakes the 89 MB CLIP model into the
image). Railway then waits up to 300 s for `/health` before switching traffic.

## Generate the public domain

Dashboard > service > Settings > Networking > Generate Domain. Note the
`https://<service>.up.railway.app` URL; point the frontend at it if needed.

## Smoke checks

```bash
BASE=https://<service>.up.railway.app
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/health"   # expect 200
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/docs"     # expect 404 (ENV=production hides Swagger)
```

Newman (from the repo root, against the deployed API with `isProduction=true`;
Swagger must then be a 404). The collections live in `postman/`:

Fill a local, untracked environment file (`local.env.json`) with the test
accounts' tokens, `ownerId`, `otherOwnerId`, `dogId`, `otherDogId`,
`otherDogName` (see `postman/README.md`), then run every collection:

```bash
for c in security inicio explorar mis-paseos perfil; do
  npx newman run postman/cantrack-$c.postman_collection.json -e local.env.json \
    --env-var isProduction=true --env-var baseUrl="$BASE"
done
```

Pass line: every assertion green, and no 5xx in `railway logs`.

## Logs

```bash
railway logs
```

Or watch live logs in the dashboard: service > Deployments > View Logs.

## Rollback

Dashboard > service > Deployments, open the last good deployment, ⋯ > **Redeploy**.
That rolls back the code and its variables together. (`railway redeploy` only
re-runs the LATEST deployment, so it is not a rollback.)

## Cost notes

- The Railway trial gives $5 of credit for 30 days.
- After the trial, the Free plan applies and the app sleeps when idle (first request after sleep is slow; the `/health` healthcheck wakes it).
