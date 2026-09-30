import pytest

from cantrack_api.structures import AvlTree


def test_starts_empty_and_min_raises():
    t = AvlTree()
    assert t.size() == 0
    assert t.height() == 0
    with pytest.raises(IndexError):
        t.min()


def test_min_returns_earliest_key_and_value_as_tuple():
    t = AvlTree()
    t.insert(300, "dog-c")
    t.insert(100, "dog-a")
    t.insert(200, "dog-b")
    assert t.min() == (100, "dog-a")


def test_stays_height_balanced_after_ascending_inserts():
    t = AvlTree()
    for i in range(100):
        t.insert(i, f"dog-{i}")
    assert t.size() == 100
    assert t.height() <= 10


def test_stays_height_balanced_after_descending_inserts():
    t = AvlTree()
    for i in range(100, 0, -1):
        t.insert(i, f"dog-{i}")
    assert t.height() <= 10


def test_height_is_exact_for_small_trees():
    t = AvlTree()
    t.insert(1, "a")
    assert t.height() == 1
    t.insert(2, "b")
    assert t.height() == 2
    t.insert(3, "c")  # forces a left rotation
    assert t.height() == 2


def test_double_rotation_cases():
    right_left = AvlTree()
    for k in (10, 30, 20):
        right_left.insert(k, k)
    assert right_left.height() == 2
    assert right_left.inorder_keys() == [10, 20, 30]
    left_right = AvlTree()
    for k in (30, 10, 20):
        left_right.insert(k, k)
    assert left_right.height() == 2
    assert left_right.inorder_keys() == [10, 20, 30]


def test_find_returns_value_or_none():
    t = AvlTree()
    t.insert(150, "dog-x")
    assert t.find(150) == "dog-x"
    assert t.find(999) is None


def test_insert_existing_key_replaces_value_without_growing():
    t = AvlTree()
    t.insert(5, "old")
    t.insert(5, "new")
    assert t.size() == 1
    assert t.find(5) == "new"


def test_remove_deletes_key_and_updates_min():
    t = AvlTree()
    for k in (50, 30, 70, 20, 40, 60, 80):
        t.insert(k, f"dog-{k}")
    t.remove(30)
    assert t.find(30) is None
    assert t.size() == 6
    assert t.min() == (20, "dog-20")


def test_remove_minimum_moves_min_forward():
    t = AvlTree()
    for k in (5, 1, 9):
        t.insert(k, k)
    t.remove(1)
    assert t.min() == (5, 5)


def test_remove_root_with_two_children_keeps_order():
    t = AvlTree()
    for k in (50, 30, 70, 20, 40, 60, 80):
        t.insert(k, k)
    t.remove(50)
    assert t.inorder_keys() == [20, 30, 40, 60, 70, 80]
    assert t.size() == 6


def test_remove_rebalances():
    t = AvlTree()
    for k in range(1, 32):
        t.insert(k, k)
    for k in range(1, 16):
        t.remove(k)
    assert t.size() == 16
    assert t.height() <= 6


def test_remove_missing_key_is_a_no_op():
    t = AvlTree()
    t.insert(1, "a")
    t.remove(99)
    assert t.size() == 1


def test_inorder_keys_are_sorted_ascending():
    t = AvlTree()
    for k in (50, 10, 90, 30, 70):
        t.insert(k, f"v{k}")
    assert t.inorder_keys() == [10, 30, 50, 70, 90]


def test_string_keys_work():
    t = AvlTree()
    for k in ("pear", "apple", "mango"):
        t.insert(k, k.upper())
    assert t.min() == ("apple", "APPLE")
    assert t.inorder_keys() == ["apple", "mango", "pear"]
