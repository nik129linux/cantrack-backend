"""Walk lifecycle states, events and the transition table (FR-24)."""

from __future__ import annotations

from enum import Enum


class WalkState(str, Enum):
    """Lifecycle states a walk request can be in."""

    OPEN = "open"
    ASSIGNED = "assigned"
    IN_WALK = "in_walk"
    DONE = "done"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class WalkEvent(str, Enum):
    """Events that can move a walk from one state to another."""

    OWNER_PICK = "owner_pick"
    AUTO_PICK = "auto_pick"
    DEADLINE_EMPTY = "deadline_empty"
    OWNER_WITHDRAW = "owner_withdraw"
    CHECK_IN = "check_in"
    WALKER_CANCEL = "walker_cancel"
    NO_SHOW = "no_show"
    OWNER_CANCEL = "owner_cancel"
    CHECK_OUT = "check_out"


class IllegalTransition(Exception):
    """Raised when an event is applied to a state that does not allow it."""


_TRANSITIONS: dict[tuple[WalkState, WalkEvent], WalkState] = {
    (WalkState.OPEN, WalkEvent.OWNER_PICK): WalkState.ASSIGNED,
    (WalkState.OPEN, WalkEvent.AUTO_PICK): WalkState.ASSIGNED,
    (WalkState.OPEN, WalkEvent.DEADLINE_EMPTY): WalkState.EXPIRED,
    (WalkState.OPEN, WalkEvent.OWNER_WITHDRAW): WalkState.CANCELLED,
    (WalkState.ASSIGNED, WalkEvent.CHECK_IN): WalkState.IN_WALK,
    (WalkState.ASSIGNED, WalkEvent.WALKER_CANCEL): WalkState.OPEN,
    (WalkState.ASSIGNED, WalkEvent.NO_SHOW): WalkState.OPEN,
    (WalkState.ASSIGNED, WalkEvent.OWNER_CANCEL): WalkState.CANCELLED,
    (WalkState.IN_WALK, WalkEvent.CHECK_OUT): WalkState.DONE,
}

_TERMINAL_STATES = frozenset(
    {WalkState.DONE, WalkState.EXPIRED, WalkState.CANCELLED}
)


class WalkStateMachine:
    """State machine for the walk lifecycle (FR-24).

    Big-O:
    - next_state: O(1)
    - allowed_events: O(E) where E is the number of events
    - is_terminal: O(1)
    """

    @staticmethod
    def next_state(current: WalkState, event: WalkEvent) -> WalkState:
        """Return the state that ``current`` moves to under ``event``.

        O(1). Raises IllegalTransition if the pair is not in the table.
        """
        try:
            return _TRANSITIONS[(current, event)]
        except KeyError:
            raise IllegalTransition(
                f"Cannot apply {event.value} to a walk in state {current.value}"
            ) from None

    @staticmethod
    def allowed_events(current: WalkState) -> frozenset[WalkEvent]:
        """Return the events legal from ``current``.

        O(E) where E is the number of events.
        """
        return frozenset(
            event
            for (state, event) in _TRANSITIONS
            if state is current
        )

    @staticmethod
    def is_terminal(state: WalkState) -> bool:
        """Return whether no event leaves ``state``. O(1)."""
        return state in _TERMINAL_STATES
