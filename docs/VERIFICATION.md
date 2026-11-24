# Source synchronization verification — 2026-11-24

- Backend GitHub Actions: **1,857 tests passed** in the clean Ubuntu/Python 3.12
  environment, including generated API cases, authentication, photo checks, user isolation,
  handwritten data structures and schema/RLS contracts.
- Hosted dependency audit: **no known vulnerabilities found**. The project packages
  `cantrack-api` and `cantrack-ai` have no PyPI advisory data and were skipped by the scanner.
- Backend CI evidence: https://github.com/nik129linux/cantrack-backend/actions/runs/45481626329
- Frontend: 524 Vitest tests passed, strict TypeScript passed, and production/PWA build passed.
  The separate frontend repository's hosted checks also passed before integration.
- AI package: 85 passed; four model-dependent tests skipped.
- Owner and walker home previews were reviewed at 390 × 844.
- Gitleaks pre-commit checks passed. Token-like examples in the historical vulnerability
  report were redacted before committing; production secrets were not added.

The CI evidence above validates the runtime source; subsequent delivery-link/documentation
adjustments do not change the tested application code.

These checks do not establish a fresh Docker build, live AI correctness, real device GPS,
or full physical iOS/Android behavior. SQL was reviewed and read by test fakes; no production
migration, seed, AWS deployment, or credential change was performed.

Remaining warnings: Starlette's TestClient warns about future httpx compatibility, four
AI tests are skipped, and Vite reports a JavaScript chunk larger than 500 kB.
