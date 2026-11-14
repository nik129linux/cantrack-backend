# CanTrack hand-written structures

One line each, from the code that actually calls them.

| Structure | Module | Endpoint | Cost of what the app uses |
|---|---|---|---|
| AVL tree | `avl_tree` | `GET /routes/next-pickup` | insert O(log n), min O(log n) |
| Circular list | `circular_list` | not wired to an endpoint | add/next O(1) |
| Doubly linked list | `doubly_list` | `GET /dogs/{dog_id}/timeline` | append O(1), reverse read O(n) |
| Interval tree | `interval_tree` | `POST /requests/{request_id}/accept` | insert O(log n), search O(log n + k) |
| Max heap | `max_heap` | not wired to an endpoint | push/pop O(log n) |
| Quadtree | `quadtree` | `GET /bidding/nearby-walkers` | insert O(depth), query O(n) worst case |
| Queue | `queue` | `GET /requests` | enqueue/dequeue O(1) |
| Rating window | `rating_window` | `GET /bidding/requests/{request_id}/offers` | add/average O(1) |
| Singly linked list | `singly_list` | `POST /routes` | append O(1), rebuild O(n) |
| Stack | `stack` | `DELETE /routes/{route_id}/checkin/undo` | push/pop O(1) |
| Token bucket | `token_bucket` | `POST /bidding/requests/{request_id}/walker-cancel` | take/available O(1) |
| Union-Find | `union_find` | `POST /plans/suggest` | union/find O(alpha(n)) |
| Walk state machine | `walk_state_machine` | `POST /bidding/requests/{request_id}/pick` | next_state O(1) |
| Weighted graph | `weighted_graph` | `POST /routes/path` | shortest_path O(V^2 + E) |

## AVL tree (`avl_tree`)

- Job: Keeps every pickup time in sorted order so the next-due stop is one leftmost descent away instead of a full scan of all stops.
- Used by: `GET /routes/next-pickup` and `apps/api-py/src/cantrack_api/routers/routes.py:294`; the bidding pick path uses it too at `apps/api-py/src/cantrack_api/routers/bidding.py:1011`.
- Cost: insert O(log n), min O(log n), inorder_keys O(n).
- Why not a list/dict: A dict gives O(1) lookup by time but cannot answer "smallest key" without scanning, and a sorted list makes every insert O(n).
- Likely professor question: "Why not a min-heap?" — A heap peeks the minimum in O(1), but the route view needs every stop in chronological order, which is one O(n) in-order walk of the AVL tree; a heap cannot also look up an arbitrary pickup time in O(log n).

## Circular list (`circular_list`)

- Job: A round-robin cursor over recurring routes, so the schedule can advance to the next route without keeping an index.
- Used by: not wired to an endpoint. The class is defined at `apps/api-py/src/cantrack_api/structures/circular_list.py:24` and only the tests import it (`tests/py/structures/test_circular_list.py`).
- Cost: add O(1), current O(1), next O(1).
- Why not a list/dict: A list needs an index plus a modulo on every advance, while the closed cycle makes "next" one pointer read; a dict has no order to rotate through.
- Likely professor question: "Does `next()` ever loop forever?" — No: one call moves exactly one link, and the tail points back to the head, so repeated calls visit every route in turn and wrap.

## Doubly linked list (`doubly_list`)

- Job: Holds a dog's or walk's timeline in time order and reads it forwards or backwards with no copy.
- Used by: `GET /dogs/{dog_id}/timeline` at `apps/api-py/src/cantrack_api/routers/checkouts.py:1092`, and `GET /bidding/requests/{request_id}/replay` through `apps/api-py/src/cantrack_api/replay.py:44`.
- Cost: append O(1), to_array O(n), to_array_reverse O(n).
- Why not a list/dict: Reversing a plain list for `order=desc` copies the whole timeline, while the previous pointers let the same nodes be read backwards in O(n) with no extra storage.
- Likely professor question: "Are the previous pointers really needed?" — Yes for `to_array_reverse` and for unlinking a known node, which needs its predecessor; a singly linked list cannot walk backwards at all.

## Interval tree (`interval_tree`)

- Job: Answers "does this walk overlap any walk I already accepted?" over the half-open 60-minute windows.
- Used by: `POST /requests/{request_id}/accept` at `apps/api-py/src/cantrack_api/routers/requests.py:462`.
- Cost: insert O(log n), search O(log n + k); O(n) worst case when every interval overlaps.
- Why not a list/dict: Scanning every accepted window is O(n) per accept, and a dict keyed by start still cannot prune by end; the max-end augmentation skips whole subtrees.
- Likely professor question: "Why AVL instead of a plain BST?" — 5000 ascending inserts turn a plain BST into a 5000-deep chain and the recursive overlap walk raises RecursionError; AVL rotations keep the height O(log n), about 13 for 5000 nodes.

## Max heap (`max_heap`)

- Job: Ranks offers by priority so the best live offer is always at the root and can be popped first, with expired offers discarded lazily.
- Used by: not wired to an endpoint. The class is defined at `apps/api-py/src/cantrack_api/structures/max_heap.py:35` and is only exercised by `tests/py/structures/test_max_heap.py`.
- Cost: push O(log n), pop_max O(log n) amortized, peek_max O(1) plus expired discards.
- Why not a list/dict: Picking the best offer from a list is O(n) per pick and a dict has no ordering, while the heap keeps the maximum at index 0 and restores order in O(log n).
- Likely professor question: "Why lazy expiry instead of deleting expired offers?" — Deleting an arbitrary entry needs its index, which a heap does not track; discarding only at the top touches each expired entry once, so the amortized cost stays O(log n).

## Quadtree (`quadtree`)

- Job: Finds walkers near a pickup point on a flat east/north plane without comparing every stored position.
- Used by: `GET /bidding/nearby-walkers` at `apps/api-py/src/cantrack_api/routers/explore.py:491`; `GET /bidding/requests/open` uses it too at `apps/api-py/src/cantrack_api/routers/bidding.py:489`.
- Cost: insert O(depth), query_radius O(n) worst case and typically O(depth + k), remove O(depth + node_capacity).
- Why not a list/dict: A list checks every walker against the pickup, while the quadtree prunes whole rectangles that cannot intersect the radius circle, so usually only nearby quadrants are visited.
- Likely professor question: "What if every point is in one corner?" — Insertion stops splitting at max_depth 12, so the cluster lives in one deep leaf and the query degrades to O(n) but the recursion depth stays bounded.

## Queue (`queue`)

- Job: Buffers pending requests in arrival order so the walker's inbox is FIFO regardless of storage order.
- Used by: `GET /requests` at `apps/api-py/src/cantrack_api/routers/requests.py:328`.
- Cost: enqueue O(1), dequeue O(1), is_empty O(1).
- Why not a list/dict: `list.pop(0)` shifts every remaining element, making inbox draining O(n^2); head and tail pointers keep both ends O(1), and a dict has no arrival order.
- Likely professor question: "Why not `collections.deque`?" — The project rule is hand-written structures only (NFR-03); this queue is the minimal hand-written FIFO with O(1) enqueue and dequeue.

## Rating window (`rating_window`)

- Job: Keeps the mean of a walker's last 25 ratings plus the all-time mean without storing unbounded rating history.
- Used by: `GET /bidding/requests/{request_id}/offers` through `apps/api-py/src/cantrack_api/routers/bidding.py:613`.
- Cost: add O(1), recent_average O(1), all_time_average O(1).
- Why not a list/dict: A plain list would grow forever and recomputing the mean on each read is O(n); the fixed buffer plus running sums give O(1) time and O(1) memory.
- Likely professor question: "How is the average still right after the window wraps?" — When the buffer is full, the rating about to be overwritten is subtracted from the running sum before the new one is added, so the sum always covers exactly the 25 buffered ratings.

## Singly linked list (`singly_list`)

- Job: Holds a route's stops in walking order and rebuilds the order from stored rows without index arithmetic.
- Used by: `POST /routes` at `apps/api-py/src/cantrack_api/routers/routes.py:397`; the bidding pick path appends the winner's stop through `apps/api-py/src/cantrack_api/routers/bidding.py:999`.
- Cost: append O(1), to_array O(n), insert_at/remove_at O(n) because they walk to the index.
- Why not a list/dict: A dict loses the stop order, and rebuilding a route from storage is a single pass of tail appends; the linked list makes the traversal explicit and keeps append O(1).
- Likely professor question: "If append is O(1), why is indexed access O(n)?" — There is no index table, so `get(i)` follows `next` i times; the endpoints always consume the whole route with `to_array` in O(n), never one random lookup per stop.

## Stack (`stack`)

- Job: Reverses the stored check-in history so the newest check-in is the first one popped for undo.
- Used by: `DELETE /routes/{route_id}/checkin/undo` at `apps/api-py/src/cantrack_api/routers/routes.py:711`.
- Cost: push O(1), pop O(1), peek O(1).
- Why not a list/dict: A stack encodes the invariant that only the most recent check-in may be undone; a list invites indexing into older rows and a dict has no notion of top.
- Likely professor question: "Why push every row just to pop one?" — Pushing oldest-first makes the top the newest row, mirroring the real undo history; if undo were allowed repeatedly, the same stack would keep yielding the next-newest check-in.

## Token bucket (`token_bucket`)

- Job: Enforces the weekly cancellation budget so a walker cannot cancel unlimited walks.
- Used by: `POST /bidding/requests/{request_id}/walker-cancel` through `apps/api-py/src/cantrack_api/routers/bidding.py:1254`.
- Cost: available O(1), take O(1).
- Why not a list/dict: The budget is three scalars plus a last-seen timestamp; storing every spend event in a list would grow without bound, while the bucket derives the refill from time in O(1).
- Likely professor question: "Why refill the whole bucket instead of dripping tokens in?" — The product rule is a weekly allowance, so crossing a boundary restores capacity; this keeps the state O(1) and lets the stored `remaining` count replay the bucket.

## Union-Find (`union_find`)

- Job: Groups compatible dogs into packs transitively, so a pickup plan knows which dogs may share a group.
- Used by: `POST /plans/suggest` through `apps/api-py/src/cantrack_api/routers/plans.py:158`; `GET /bidding/requests/open` uses it too at `apps/api-py/src/cantrack_api/routers/bidding.py:460`.
- Cost: add O(1) amortized, find/union O(alpha(n)) amortized.
- Why not a list/dict: Testing connectivity by repeated graph searches is expensive; Union-Find merges as pairs are discovered and answers "same component" in near-constant amortized time.
- Likely professor question: "Why union by size and path compression together?" — Either alone gives O(log n), while together the amortized cost is O(alpha(n)); the find is iterative, so a 100 000-long chain cannot overflow the stack.

## Walk state machine (`walk_state_machine`)

- Job: Centralizes the legal walk status transitions so no endpoint can move a walk out of an invalid state.
- Used by: `POST /bidding/requests/{request_id}/pick` through `apps/api-py/src/cantrack_api/routers/bidding.py:1205` (the shared `_validate_transition` helper used by the bidding endpoints).
- Cost: next_state O(1); allowed_events O(E) over the number of events.
- Why not a list/dict: A bare dict would expose the table for mutation and silently return `None` on an illegal pair; the class wraps the same dict with typed states and an `IllegalTransition` exception.
- Likely professor question: "Does this scale to new states?" — The table is a fixed 9-entry dict keyed by `(state, event)`, so lookup stays O(1) and adding a transition is one enum value plus one table row, not new branching code.

## Weighted graph (`weighted_graph`)

- Job: Finds the cheapest walking route between stops using the street graph's minute weights.
- Used by: `POST /routes/path` at `apps/api-py/src/cantrack_api/routers/routes.py:338`; the walker day route uses it for each leg at `apps/api-py/src/cantrack_api/routers/walker_route.py:256`.
- Cost: add_edge O(1) average, shortest_path O(V^2 + E) with the simple Dijkstra.
- Why not a list/dict: A dict of edges alone cannot answer "cheapest path"; the adjacency list plus Dijkstra visits reachable legs only and keeps one distance per node.
- Likely professor question: "Why O(V^2 + E) instead of O((V+E) log V)?" — The unvisited scan picks the next minimum without a heap, which is simpler and fine for the campus-sized graph; a binary heap would add heap operations and tie-breaking complexity for little gain here.
