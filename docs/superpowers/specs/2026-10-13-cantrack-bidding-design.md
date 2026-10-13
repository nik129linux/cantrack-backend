# CanTrack — Walk bidding, reputation and replay (design)

Date: 2026-10-13. Status: approved by Nico in session. Source research:
`~/cantrack-split/research/indrive-recon/` (public InDrive recon + `gap.md`).

## Why

The professor lifted the "no marketplace" restriction (recorded in `REQUIREMENTS.md`, out of
scope section). CanTrack moves from walker-built routes only to an InDrive-style flow: the owner
posts a walk and nearby walkers bid on it. The design reuses the structures already built and
adds five new hand-written ones.

## Flow

```
Owner posts a walk request: dog(s), duration in hours, time window, price, pickup point
        │
        ▼
Walker Quadtree ── walkers within radius R see the request
(second instance of the FR-16 Quadtree class: FR-16 indexes pending dogs for a walker,
 this one indexes walker positions for a request)
        │
        ▼
Eligibility per walker (FR-25)
  free in the window (Interval Tree, FR-17)            → may bid
  busy in the window → all three must pass:
     pack-compatible with current dogs (Union-Find, FR-15)
     detour to the new pickup ≤ D minutes (Dijkstra, FR-09)
     dog count stays ≤ walker capacity
  otherwise                                            → request hidden from that walker
        │
        ▼
Walkers send offers (price, ETA). Offers go into a max-heap (FR-23) by score.
        │
        ▼
Walk state machine (FR-24), every other transition raises:
  OPEN     → ASSIGNED   owner picks an offer, or at deadline X the top of the heap is auto-picked
  OPEN     → EXPIRED    deadline X reached with an empty heap
  OPEN     → CANCELLED  owner withdraws the request
  ASSIGNED → IN_WALK    walker's first check-in (FR-10)
  ASSIGNED → OPEN       walker cancels: costs one cancel token (FR-26), the request reopens
                        with the remaining offers still in the heap
  ASSIGNED → CANCELLED  owner cancels before pickup
  ASSIGNED → OPEN       no-show: no check-in within 15 min of the window start; counts as a
                        walker cancellation (one token)
  IN_WALK  → DONE       walker checks out (an early end is a check-out with an incident event
                        on the timeline, FR-12)
  Precedence: the transition out of OPEN is a single compare-and-set on the walk's state, so the
  first of {owner pick, auto-pick at X} to apply wins; the other gets "walk no longer open".
  An owner pick before X always wins because auto-pick only fires at X.
        │
        ▼
A busy walker who wins gets the new dog inserted as a stop in the active route
(singly linked list, FR-06); AVL (FR-08) re-indexes the next pickup.
```

## New structures (all hand-written, NFR-03/04/12)

| Structure | Used for | Key operations |
|---|---|---|
| Binary max-heap | Offer ranking per walk request | push O(log n), pop-max O(log n) amortized (each expired offer is discarded once, so e expired offers add O(e log n) in total, not per pop), peek O(1) |
| Quadtree | Walkers near a pickup point | insert O(depth), radius query O(n) worst case, typically O(depth + k); depth is O(log n) for evenly spread walkers, O(n) worst case, so a max depth caps it |
| State machine (transition table) | Walk lifecycle, single winner | transition O(1); illegal transition raises |
| Fixed-capacity circular buffer | Last-25 rating window | push O(1), running average O(1) |
| Token bucket | Cancellation limit (3 per week) | take O(1), refill O(1) on read |

Offer score = weighted sum of price, distance and recent rating (window average). Weights live in
one config object so they can be tuned without touching the heap.

## Reputation (FR-26)

- The owner sees the **all-time** rating (running sum / count).
- The heap ranks by the **last 25 ratings** (circular buffer), so recent behaviour counts.
- While the walker's token bucket is empty, their offer score is multiplied by 0.8.

## Walk replay (FR-27)

The owner scrubs through a finished walk. Events (check-in, photo, incident, check-out) are
navigated forward and backward on the doubly linked list (FR-12). GPS points come from the walk's
array time series (FR-14); jumping to a timestamp uses binary search over that sorted array,
O(log n). FR-21's daily report is generated from the same timeline.

## Naming convention (professor's request)

Names describe the domain, so the code reads like the problem. Examples (not a closed list):
`walk_request`, `offer`, `walker`, `pending_dogs`, `nearby_walkers`, `offer_heap`,
`rating_window`, `cancel_bucket`.
No `x`, `tmp`, `data`, `item`, `obj` outside a structure's own internals. Generic structures keep
generic internals (`node`, `left`, `right`) but every call site names the instance by its domain
role. Recorded as NFR-13.

## Testing

- Unit tests per new structure (NFR-12), including edge cases: empty heap pop, quadtree query
  on an empty region, illegal state transition, buffer wrap-around at 26 ratings, empty bucket.
- Integration: two walkers accept the same OPEN walk concurrently, exactly one wins.
- Integration: one busy walker passes the detour check and is offered the walk; another exceeds
  D minutes and never sees it.

## Build order

1. Heap + walk state machine (the core of bidding).
2. Quadtree (FR-16) and nearby-walker query.
3. Busy-walker eligibility (Interval Tree + Union-Find + Dijkstra + capacity).
4. Reputation (circular buffer + token bucket) feeding the score.
5. Replay + FR-21 report.

## Cut to make room

FR-18 (Trie autocomplete), FR-20 (voice incidents), FR-22 (stats dashboard).

## Default values (one config object, tunable)

Radius R = 2 km · detour limit D = 10 min · auto-pick deadline X = 10 min after posting ·
walker capacity = 4 dogs · score = 0.4 price + 0.3 distance + 0.3 recent rating, each min-max normalised to 0..1 across
the offers on the same walk request (lower price and distance score higher; a single offer
scores 1 on each term; ties broken by earliest offer) · cancel budget = 3 tokens, refilled weekly.
