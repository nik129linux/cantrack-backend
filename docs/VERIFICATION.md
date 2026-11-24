# Source synchronization verification — 2026-11-24

- Backend run excluding generated schema cases: 1,645 passed and one failed because a
  strict response expectation predated the added `photos` field. The expected response
  now includes `photos: []`; 156 related client/request/walk tests passed after correction.
- Schemathesis API cases: 211 passed.
- AI package: 85 passed and four model-dependent tests skipped.
- Complete-repository Python run: 1,941 passed, four skipped and one workflow-contract
  failure. The workflow's compatibility checkout was restored; 19 affected workflow,
  RLS/schema and client-edge tests then passed. The hosted CI run checks the full final tree.
- Dependency audit of the existing local backend environment: no known vulnerabilities;
  the project packages `cantrack-api` and `cantrack-ai` have no PyPI advisory data and were skipped.
- Owner and walker home previews were reviewed at 390 × 844; frontend tests (524), strict
  TypeScript and production/PWA build passed in the complete repository.

Validation used the existing local Python environment and frontend dependency installation.
It does not establish a clean Docker build, live AI correctness, real device GPS, or full
physical iOS/Android behavior. SQL was reviewed and read by the test fakes; no production
migration, seed, AWS deployment, or credential change was performed.

Remaining warnings: Starlette's TestClient warns about future httpx compatibility, four
AI tests are skipped, and Vite reports a JavaScript chunk larger than 500 kB.
