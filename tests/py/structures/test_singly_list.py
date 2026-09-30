import pytest

from cantrack_api.structures import SinglyLinkedList


def make(*items):
    lst = SinglyLinkedList()
    for item in items:
        lst.append(item)
    return lst


def test_starts_empty():
    lst = SinglyLinkedList()
    assert lst.size() == 0
    assert lst.to_array() == []


def test_appends_in_order():
    lst = make("dog-a", "dog-b", "dog-c")
    assert lst.to_array() == ["dog-a", "dog-b", "dog-c"]
    assert lst.size() == 3


def test_insert_at_middle():
    lst = make("dog-a", "dog-c")
    lst.insert_at(1, "dog-b")
    assert lst.to_array() == ["dog-a", "dog-b", "dog-c"]


def test_insert_at_head_and_tail():
    lst = make("b")
    lst.insert_at(0, "a")
    lst.insert_at(2, "c")
    assert lst.to_array() == ["a", "b", "c"]
    lst.append("d")
    assert lst.to_array() == ["a", "b", "c", "d"]


def test_insert_at_into_empty_list():
    lst = SinglyLinkedList()
    lst.insert_at(0, "only")
    assert lst.to_array() == ["only"]
    assert lst.size() == 1


def test_remove_at_middle_head_and_tail():
    lst = make("a", "b", "c", "d")
    lst.remove_at(1)
    assert lst.to_array() == ["a", "c", "d"]
    lst.remove_at(0)
    assert lst.to_array() == ["c", "d"]
    lst.remove_at(1)
    assert lst.to_array() == ["c"]
    lst.append("z")
    assert lst.to_array() == ["c", "z"]


def test_remove_last_remaining_item_then_append():
    lst = make("a")
    lst.remove_at(0)
    assert lst.size() == 0
    lst.append("b")
    assert lst.to_array() == ["b"]


@pytest.mark.parametrize("index", [5, -1])
def test_remove_at_out_of_range_raises(index):
    with pytest.raises(IndexError):
        make("dog-a").remove_at(index)


@pytest.mark.parametrize("index", [5, -1])
def test_insert_at_out_of_range_raises(index):
    with pytest.raises(IndexError):
        make("dog-a").insert_at(index, "dog-x")


def test_get_returns_value_at_index():
    lst = make(10, 20)
    assert lst.get(1) == 20


@pytest.mark.parametrize("index", [2, -1])
def test_get_out_of_range_raises(index):
    with pytest.raises(IndexError):
        make(10, 20).get(index)
