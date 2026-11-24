# Deployment and external configuration

This repository contains source code, not a backup of production users, photos, credentials or cloud resources.
No cloud service or database is changed by cloning or building it.

## Existing AWS deployment

- Web/PWA: https://d2p0dna7twnh6n.cloudfront.net
- API: https://cantrack-demo-api.ccchtce26hsxy.us-east-1.cs.amazonlightsail.com
- Health: the API URL followed by `/health`.
- Web hosting: private S3 bucket behind CloudFront.
- Backend: Lightsail container service `cantrack-demo-api`, region `us-east-1`.
- Database, authentication and private photo storage: Supabase.
- Last recorded production release: 2026-11-23, image tag `walk-photo-ai-20261123`.
  That release predates the complete source synchronization and security updates of 2026-11-24.

## Prepare a new environment

1. Create a Supabase project. Run `cantrack-db/apps/api-py/schema.sql` for a fresh database.
   Existing databases use the numbered migrations in order and the supplemental SQL described
   in the database README. Review SQL before execution. The backend expects private `avatars`
   and `checkout-photos` storage buckets; the schema creates them.
2. Configure Supabase email/authentication and redirect URLs for the intended web domain.
   The schema does not recreate Auth users, existing photo objects, or seed credentials.
3. Copy the backend `.env.example` to `.env` and supply `SUPABASE_URL` and
   `SUPABASE_SERVICE_ROLE_KEY` through your hosting platform's secret configuration.
   Set `ENV=production`, `PORT=3000` and `WEB_ORIGIN` to the HTTPS web origin.
4. Set `AI_CHAIN=gemini,groq` (or the desired order) and provide the corresponding
   `GEMINI_API_KEY` and/or `GROQ_API_KEY`. Do not expose these keys to the browser.
   Provider failures are advisory for photo checks; the source tests use fakes.
5. Build the API from `apps/api-py`: `docker build -t cantrack-api apps/api-py`.
   The Dockerfile installs the locked Python dependencies and downloads the CLIP model.
   A fresh build requires internet access to package registries, GitHub and the model host.
6. Run one API process/replica with its environment and expose port 3000 behind HTTPS.
   Rate limits are stored in process memory. Only trust forwarded-IP headers that your proxy overwrites.
7. Configure the frontend's three `VITE_*` variables, install its locked dependencies,
   run `npm run typecheck` and `npm run build`, and publish `apps/web/dist` to static hosting.
8. Serve `index.html` and the service worker with revalidation/no-cache; hashed assets can be immutable.
   Invalidate cached entry files after publishing. Preserve `.well-known/assetlinks.json` for the Android TWA.
9. Verify `/health`, login, both account modes, private photo loading, GPS permissions and pickup/return flows.
   Real AI, device GPS and physical iOS/Android behavior require separate end-to-end checks.

The existing APK is associated with the CloudFront hostname. A different domain requires a new Android
association/build. The Android signing keystore is intentionally not part of these repositories.

## Local development

Backend: `cd apps/api-py`, `uv sync --frozen`, then `uv run --no-sync python -m cantrack_api.main`.
Use `WEB_ORIGIN=http://localhost:5173` and leave `ENV` unset locally.
Frontend: configure `.env`, run `npx pnpm@9 install --frozen-lockfile`, then `npm run dev`.
Never upload real `.env`, service-role keys, provider tokens or Android signing files.
