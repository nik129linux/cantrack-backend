import pytest

from cantrack_api.structures import Queue, Stack


class TestStack:
    def test_starts_empty(self):
        s = Stack()
        assert s.is_empty() is True
        assert s.size() == 0

    def test_pushes_and_pops_in_lifo_order(self):
        s = Stack()
        s.push("check-in-1")
        s.push("check-in-2")
        s.push("check-in-3")
        assert s.pop() == "check-in-3"
        assert s.pop() == "check-in-2"
        assert s.size() == 1

    def test_peek_returns_top_without_removing(self):
        s = Stack()
        s.push(1)
        s.push(2)
        assert s.peek() == 2
        assert s.size() == 2

    def test_pop_on_empty_raises_index_error(self):
        with pytest.raises(IndexError):
            Stack().pop()

    def test_peek_on_empty_raises_index_error(self):
        with pytest.raises(IndexError):
            Stack().peek()

    def test_none_is_a_storable_value(self):
        s = Stack()
        s.push(None)
        assert s.is_empty() is False
        assert s.pop() is None


class TestQueue:
    def test_starts_empty(self):
        q = Queue()
        assert q.is_empty() is True
        assert q.size() == 0

    def test_enqueues_and_dequeues_in_fifo_order(self):
        q = Queue()
        q.enqueue("point-1")
        q.enqueue("point-2")
        q.enqueue("point-3")
        assert q.dequeue() == "point-1"
        assert q.dequeue() == "point-2"
        assert q.size() == 1

    def test_front_returns_head_without_removing(self):
        q = Queue()
        q.enqueue(10)
        q.enqueue(20)
        assert q.front() == 10
        assert q.size() == 2

    def test_dequeue_on_empty_raises_index_error(self):
        with pytest.raises(IndexError):
            Queue().dequeue()

    def test_front_on_empty_raises_index_error(self):
        with pytest.raises(IndexError):
            Queue().front()

    def test_interleaved_enqueue_dequeue_keeps_order(self):
        q = Queue()
        q.enqueue(1)
        q.enqueue(2)
        assert q.dequeue() == 1
        q.enqueue(3)
        assert q.dequeue() == 2
        assert q.dequeue() == 3
        assert q.is_empty() is True

    def test_many_items_keep_fifo_order(self):
        q = Queue()
        for i in range(1000):
            q.enqueue(i)
        assert [q.dequeue() for _ in range(1000)] == list(range(1000))
