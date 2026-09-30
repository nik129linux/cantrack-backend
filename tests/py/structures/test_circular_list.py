import pytest

from cantrack_api.structures import CircularList


def make(*items):
    c = CircularList()
    for item in items:
        c.add(item)
    return c


def test_starts_empty():
    assert CircularList().size() == 0


def test_next_on_empty_raises():
    with pytest.raises(IndexError):
        CircularList().next()


def test_current_on_empty_raises():
    with pytest.raises(IndexError):
        CircularList().current()


def test_reads_in_order_without_wrapping():
    c = make("route-A", "route-B", "route-C")
    assert c.current() == "route-A"
    assert c.next() == "route-B"
    assert c.next() == "route-C"
    assert c.current() == "route-C"


def test_wraps_to_first_after_last():
    c = make("route-A", "route-B")
    c.next()
    assert c.next() == "route-A"
    assert c.current() == "route-A"


def test_full_rotation_visits_every_item_then_repeats():
    c = make(1, 2, 3)
    seen = [c.next() for _ in range(6)]
    assert seen == [2, 3, 1, 2, 3, 1]


def test_single_item_wraps_to_itself():
    c = make("only-route")
    assert c.next() == "only-route"
    assert c.next() == "only-route"


def test_size_reflects_additions():
    assert make(1, 2).size() == 2


def test_add_after_rotating_appends_at_the_end_of_the_cycle():
    c = make("a", "b")
    c.next()  # cursor on b
    c.add("c")
    assert c.next() == "c"
    assert c.next() == "a"
