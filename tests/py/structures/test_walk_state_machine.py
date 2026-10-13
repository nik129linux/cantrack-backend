import pytest

from cantrack_api.structures import (
    IllegalTransition,
    WalkEvent,
    WalkState,
    WalkStateMachine,
)

LEGAL = [
    (WalkState.OPEN, WalkEvent.OWNER_PICK, WalkState.ASSIGNED),
    (WalkState.OPEN, WalkEvent.AUTO_PICK, WalkState.ASSIGNED),
    (WalkState.OPEN, WalkEvent.DEADLINE_EMPTY, WalkState.EXPIRED),
    (WalkState.OPEN, WalkEvent.OWNER_WITHDRAW, WalkState.CANCELLED),
    (WalkState.ASSIGNED, WalkEvent.CHECK_IN, WalkState.IN_WALK),
    (WalkState.ASSIGNED, WalkEvent.WALKER_CANCEL, WalkState.OPEN),
    (WalkState.ASSIGNED, WalkEvent.NO_SHOW, WalkState.OPEN),
    (WalkState.ASSIGNED, WalkEvent.OWNER_CANCEL, WalkState.CANCELLED),
    (WalkState.IN_WALK, WalkEvent.CHECK_OUT, WalkState.DONE),
]


class TestWalkStateMachine:
    @pytest.mark.parametrize("current,event,expected", LEGAL)
    def test_legal_transitions(self, current, event, expected):
        assert WalkStateMachine.next_state(current, event) == expected

    def test_every_other_pair_is_illegal(self):
        legal_pairs = {(current, event) for current, event, _ in LEGAL}
        for current in WalkState:
            for event in WalkEvent:
                if (current, event) in legal_pairs:
                    continue
                with pytest.raises(IllegalTransition):
                    WalkStateMachine.next_state(current, event)

    def test_second_pick_on_assigned_walk_is_rejected(self):
        with pytest.raises(IllegalTransition):
            WalkStateMachine.next_state(WalkState.ASSIGNED, WalkEvent.OWNER_PICK)

    @pytest.mark.parametrize(
        "terminal", [WalkState.DONE, WalkState.EXPIRED, WalkState.CANCELLED]
    )
    def test_terminal_states_allow_nothing(self, terminal):
        assert WalkStateMachine.is_terminal(terminal) is True
        assert WalkStateMachine.allowed_events(terminal) == frozenset()

    def test_open_is_not_terminal(self):
        assert WalkStateMachine.is_terminal(WalkState.OPEN) is False

    def test_allowed_events_for_assigned(self):
        assert WalkStateMachine.allowed_events(WalkState.ASSIGNED) == frozenset(
            {
                WalkEvent.CHECK_IN,
                WalkEvent.WALKER_CANCEL,
                WalkEvent.NO_SHOW,
                WalkEvent.OWNER_CANCEL,
            }
        )

    def test_allowed_events_for_open_and_in_walk(self):
        assert WalkStateMachine.allowed_events(WalkState.OPEN) == frozenset(
            {
                WalkEvent.OWNER_PICK,
                WalkEvent.AUTO_PICK,
                WalkEvent.DEADLINE_EMPTY,
                WalkEvent.OWNER_WITHDRAW,
            }
        )
        assert WalkStateMachine.allowed_events(WalkState.IN_WALK) == frozenset(
            {WalkEvent.CHECK_OUT}
        )

    @pytest.mark.parametrize("live", [WalkState.ASSIGNED, WalkState.IN_WALK])
    def test_live_states_are_not_terminal(self, live):
        assert WalkStateMachine.is_terminal(live) is False

    def test_states_round_trip_through_database_strings(self):
        assert WalkState("in_walk") is WalkState.IN_WALK

    def test_illegal_transition_message_names_both_sides(self):
        with pytest.raises(IllegalTransition, match="done.*check_in|check_in.*done"):
            WalkStateMachine.next_state(WalkState.DONE, WalkEvent.CHECK_IN)
