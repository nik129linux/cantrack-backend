import random

import pytest

from cantrack_api.structures import DoublyLinkedList


def make(*items):
    lst = DoublyLinkedList()
    for item in items:
        lst.append(item)
    return lst


def test_starts_empty():
    lst = DoublyLinkedList()
    assert lst.size() == 0
    assert lst.to_array() == []
    assert lst.to_array_reverse() == []


def test_append_reads_forward():
    lst = make("check-in", "incident: barking", "check-out")
    assert lst.to_array() == ["check-in", "incident: barking", "check-out"]


def test_reads_backward():
    lst = make("check-in", "incident: barking", "check-out")
    assert lst.to_array_reverse() == ["check-out", "incident: barking", "check-in"]


def test_prepend_at_front():
    lst = make("check-out")
    lst.prepend("check-in")
    assert lst.to_array() == ["check-in", "check-out"]
    assert lst.to_array_reverse() == ["check-out", "check-in"]


def test_prepend_into_empty_list():
    lst = DoublyLinkedList()
    lst.prepend("a")
    lst.append("b")
    assert lst.to_array() == ["a", "b"]
    assert lst.to_array_reverse() == ["b", "a"]


def test_remove_at_middle_keeps_both_links_consistent():
    lst = make("a", "b", "c")
    lst.remove_at(1)
    assert lst.to_array() == ["a", "c"]
    assert lst.to_array_reverse() == ["c", "a"]


def test_remove_head_and_tail_keep_links_consistent():
    lst = make("a", "b", "c", "d")
    lst.remove_at(0)
    lst.remove_at(2)
    assert lst.to_array() == ["b", "c"]
    assert lst.to_array_reverse() == ["c", "b"]
    lst.append("e")
    lst.prepend("z")
    assert lst.to_array() == ["z", "b", "c", "e"]
    assert lst.to_array_reverse() == ["e", "c", "b", "z"]


def test_remove_only_item_leaves_usable_empty_list():
    lst = make("a")
    lst.remove_at(0)
    assert lst.size() == 0
    assert lst.to_array() == []
    assert lst.to_array_reverse() == []
    lst.append("b")
    assert lst.to_array_reverse() == ["b"]


@pytest.mark.parametrize("index", [9, -1])
def test_remove_at_out_of_range_raises(index):
    with pytest.raises(IndexError):
        make("a").remove_at(index)


class TestScaleAndRandomized:
    """S3 brief: the timeline is a DoublyLinkedList, so 10 000 events must
    keep exact order in both directions without recursion, and random
    append/prepend/remove sequences must match a brute-force list."""

    def test_10_000_appends_keep_exact_order_both_ways(self):
        dll = DoublyLinkedList()
        for i in range(10_000):
            dll.append(i)
        assert dll.size() == 10_000
        assert dll.to_array() == list(range(10_000))
        assert dll.to_array_reverse() == list(range(9_999, -1, -1))

    def test_10_000_prepends_keep_exact_order_both_ways(self):
        dll = DoublyLinkedList()
        for i in range(10_000):
            dll.prepend(i)
        assert dll.to_array() == list(range(9_999, -1, -1))
        assert dll.to_array_reverse() == list(range(10_000))

    def test_randomized_ops_match_a_brute_force_list(self):
        rng = random.Random(20261104)
        dll = DoublyLinkedList()
        model: list[int] = []
        for step in range(500):
            if rng.random() < 0.5:
                dll.append(step)
                model.append(step)
            else:
                dll.prepend(step)
                model.insert(0, step)
            if step % 50 == 0:
                assert dll.to_array() == model
                assert dll.to_array_reverse() == list(reversed(model))
        while model:
            index = rng.randrange(len(model))
            dll.remove_at(index)
            del model[index]
            assert dll.to_array() == model
        assert dll.size() == 0
        assert dll.to_array() == []
        assert dll.to_array_reverse() == []
