# CanTrack — Requirements

Estructuras de Datos final project, UCC 2026-2. Neighbourhood dog-walker routing and operations
software. Background and case framing: `../cantrack.md` (one level up, kept as the pitch doc).

Every requirement is tagged `[Oct5]` (must exist for the 5 Oct progress check, ≥20% of the project)
or `[Later]` (final delivery, 19 Nov). `[Oct5]` requirements are what `docs/TASKS.md` builds first.

## Functional requirements

### Accounts
- **FR-01** `[Oct5]` A person can sign up as either a **walker** or an **owner**.
- **FR-02** `[Oct5]` A person can log in with email + password.
- **FR-03** `[Oct5]` A person can request a password reset email and set a new password.

### Dogs
- **FR-04** `[Oct5]` An owner can create, read, update, and delete their own dogs (name, breed,
  notes).
- **FR-05** `[Oct5]` An owner can enroll a dog with 3 reference photos. The API embeds each photo
  (CLIP) and stores the mean embedding against the dog.

### Routes
- **FR-06** `[Oct5]` A walker can create a route: an ordered sequence of stops, each stop tied to
  one dog. Backed by a hand-written **singly linked list**.
- **FR-07** `[Oct5]` A walker can reorder or remove a stop from a route before it starts.
- **FR-08** `[Oct5]` Given a walker's active routes, the system returns the next scheduled pickup
  in O(log n), backed by a hand-written **AVL tree** keyed by pickup time.
- **FR-09** `[Oct5]` Given a route's stops and a street graph, the system computes the pickup path
  with a hand-written **weighted graph + Dijkstra**.

### Check-in
- **FR-10** `[Oct5]` At a stop, the walker takes a photo. The photo is uploaded to the API, embedded (CLIP) there and matched by cosine similarity against the dogs on that route. Above a similarity
  threshold, the match auto-confirms; below it, the walker picks manually from that route's dogs. A vision model (Ollama) also checks
  that a dog is actually in the photo and rejects the check-in when there is none; if the model is
  unavailable the check-in still proceeds.
- **FR-11** `[Oct5]` A walker can undo their most recent check-in, backed by a hand-written
  **stack**.

### Walk
- **FR-12** `[Oct5 structures / Later UI]` A walk's timeline (check-in, incident, check-out
  events) is backed by a hand-written **doubly linked list**, navigable forward and backward.
- **FR-13** `[Oct5 structures / Later UI]` A walker's recurring routes rotate through a
  hand-written **circular list**.
- **FR-14** `[Oct5 structures / Later UI]` GPS points during a walk are buffered through a
  hand-written **queue** before being persisted as an **array** time series.

### Later (final delivery only)
- **FR-15** Compatible dogs are grouped into packs using **Union-Find** with path compression.
- **FR-16** The walker's nearest pending dog is found via a 2D **Quadtree** spatial index.
- **FR-17** Owner-fixed time-window conflicts are detected with an **Interval Tree**.
- **FR-19** The owner sees the walker's live GPS position during the walk (Realtime).
- **FR-21** A daily report (photos, map, metrics) is auto-written per walk.

### Walk bidding (added 2026-10-13, design: `docs/superpowers/specs/2026-10-13-cantrack-bidding-design.md`)
- **FR-23** An owner posts a walk request (dogs, duration, time window, price, pickup point); walkers
  within radius R (Quadtree, FR-16) send offers ranked in a hand-written **binary max-heap**.
- **FR-24** A walk follows a hand-written **state machine** (OPEN, ASSIGNED, IN_WALK, DONE, EXPIRED,
  CANCELLED);
  exactly one offer can win, and a second accept is rejected.
- **FR-25** A walker busy in the requested window may still bid when the dog is pack-compatible
  (Union-Find, FR-15), the detour is at most D minutes (Dijkstra, FR-09) and capacity allows; the
  dog is then inserted as a stop in the active route (FR-06).
- **FR-26** Walker reputation: the owner sees the all-time rating; offer ranking uses the last 25
  ratings in a hand-written **circular buffer**, and cancellations are limited by a hand-written
  **token bucket**.
- **FR-27** The owner replays a finished walk: events via the doubly linked list (FR-12), GPS jump
  by binary search over the time series (FR-14).

Removed 2026-10-13 to make room: FR-18 (Trie autocomplete), FR-20 (voice incidents), FR-22
(statistics dashboard).

## Non-functional requirements

- **NFR-01** Backend in Python (FastAPI), frontend in TypeScript (React). Set by the professor in class on 2026-09-28.
- **NFR-02** All identifiers, comments, docstrings, and error/log/UI strings are in English.
- **NFR-03** `apps/api-py/src/cantrack_api/structures` imports no data-structure library — every
  data structure listed above is hand-written (only `typing`/`dataclasses` from the standard library).
- **NFR-04** Every exported structure documents the Big-O of each operation it implements.
- **NFR-05** Free tier only end to end: Vercel (frontend), Render (backend), Supabase (DB + auth +
  realtime + pgvector), Ollama Cloud (vision model); the CLIP image model runs inside the API.
- **NFR-06** The system is deployed at a public URL for the code review.
- **NFR-07** The database is cloud-hosted (Supabase Postgres), never local-only.
- **NFR-08** Photo-to-dog check-in returns a match in under 3 seconds on a mid-range phone.
- **NFR-09** All API input is validated (pydantic) before it reaches business logic.
- **NFR-10** Secrets (Supabase keys, Ollama key) live only in environment variables, never in
  committed source.
- **NFR-11** The web app is installable as a PWA.
- **NFR-12** Each hand-written structure has its own unit tests, independent of the API/web tests.
- **NFR-13** Names describe the domain (examples: `walk_request`, `offer_heap`, `nearby_walkers`, `pending_dogs`);
  no `x`, `tmp`, `data`, `item` outside a structure's own internals (professor's request, 2026-10-13).

## Explicitly out of scope

Booking/reservation flows were banned by the professor; **lifted 2026-10-13** (see FR-23..FR-27).
Still out of scope:
any structure-library dependency in `apps/api-py/src/cantrack_api/structures` (NFR-03), and writing the graded deliverable
content itself (see `../cantrack.md` — that boundary was set 2026-08-09 and stands).
