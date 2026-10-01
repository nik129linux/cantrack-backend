"""FR-15: compatible dogs are grouped with a hand-written UnionFind.

Contract pinned here (S2):
- ``add(x)`` registers an element (idempotent); unknown elements raise
  ``KeyError`` from find/union/connected.
- ``find(x)`` returns the representative of x's set (root identity is NOT
  pinned beyond find(a) == find(b) after union — union-by-size may pick
  either side).
- ``union(a, b)`` merges sets, returning True when it merged and False when
  they were already together.
- ``connected(a, b)`` is exact; ``count()`` is the number of disjoint sets.
- Union by size/rank with ITERATIVE path compression: a 100 000-long chain
  must not raise RecursionError (the default recursion limit is 1000).
- Randomized equivalence against brute-force connected components.
"""

import random

import pytest

from cantrack_api.structures import UnionFind


class TestBasics:
    def test_starts_empty(self):
        uf = UnionFind()
        assert uf.count() == 0

    def test_add_registers_singleton_sets(self):
        uf = UnionFind()
        uf.add("a")
        uf.add("b")
        assert uf.count() == 2
        assert uf.find("a") == "a"
        assert uf.connected("a", "b") is False

    def test_add_is_idempotent(self):
        uf = UnionFind()
        uf.add("a")
        uf.add("a")
        assert uf.count() == 1

    def test_union_merges_and_reports_whether_it_merged(self):
        uf = UnionFind()
        uf.add("a")
        uf.add("b")
        assert uf.union("a", "b") is True
        assert uf.union("a", "b") is False
        assert uf.union("b", "a") is False
        assert uf.connected("a", "b") is True
        assert uf.find("a") == uf.find("b")
        assert uf.count() == 1

    def test_union_is_transitive(self):
        uf = UnionFind()
        for name in ("a", "b", "c", "d"):
            uf.add(name)
        uf.union("a", "b")
        uf.union("c", "d")
        assert uf.connected("a", "c") is False
        assert uf.count() == 2
        uf.union("b", "c")
        assert uf.connected("a", "d") is True
        assert uf.count() == 1

    def test_union_of_an_element_with_itself_does_not_change_count(self):
        uf = UnionFind()
        uf.add("a")
        assert uf.union("a", "a") is False
        assert uf.count() == 1

    @pytest.mark.parametrize("op", ["find", "connected_left", "connected_right", "union"])
    def test_unknown_elements_raise_key_error(self, op):
        uf = UnionFind()
        uf.add("a")
        with pytest.raises(KeyError):
            if op == "find":
                uf.find("ghost")
            elif op == "connected_left":
                uf.connected("ghost", "a")
            elif op == "connected_right":
                uf.connected("a", "ghost")
            else:
                uf.union("a", "ghost")

    def test_values_are_arbitrary_hashables(self):
        uf = UnionFind()
        uf.add(7)
        uf.add(("dog", 1))
        assert uf.union(7, ("dog", 1)) is True
        assert uf.connected(7, ("dog", 1)) is True


class TestRandomizedEquivalence:
    def test_matches_brute_force_components_over_500_random_unions(self):
        rng = random.Random(20261002)
        elements = [f"e{i}" for i in range(200)]

        uf = UnionFind()
        for element in elements:
            uf.add(element)

        # brute force: a list of disjoint sets
        components: list[set[str]] = [{element} for element in elements]

        def brute_connected(a: str, b: str) -> bool:
            for component in components:
                if a in component:
                    return b in component
            raise AssertionError("element missing from brute force")

        def brute_count() -> int:
            return len(components)

        for step in range(500):
            a, b = rng.choice(elements), rng.choice(elements)
            merged = uf.union(a, b)
            # apply the same union to the brute force
            comp_a = next(c for c in components if a in c)
            comp_b = next(c for c in components if b in c)
            brute_merged = comp_a is not comp_b
            if brute_merged:
                comp_a |= comp_b
                components.remove(comp_b)

            assert merged == brute_merged, f"step {step}: union({a}, {b})"
            assert uf.count() == brute_count(), f"step {step}: count"
            for _ in range(10):
                x, y = rng.choice(elements), rng.choice(elements)
                assert uf.connected(x, y) == brute_connected(x, y), (
                    f"step {step}: connected({x}, {y})"
                )

        # full pairwise agreement at the end
        for x in elements:
            for y in elements:
                assert uf.connected(x, y) == brute_connected(x, y)


class TestScale:
    def test_100_000_chain_unions_do_not_recurse(self):
        uf = UnionFind()
        n = 100_000
        for i in range(n):
            uf.add(i)
        # worst case for naive union/find: a single chain
        for i in range(n - 1):
            uf.union(i, i + 1)
        assert uf.count() == 1
        # a recursive find over a degenerate chain would need 100k frames
        assert uf.find(0) == uf.find(n - 1)
        assert uf.find(n // 2) == uf.find(0)
        assert uf.connected(0, n - 1) is True

    def test_path_compression_keeps_repeated_finds_cheap(self):
        uf = UnionFind()
        n = 50_000
        for i in range(n):
            uf.add(i)
        for i in range(n - 1):
            uf.union(i, i + 1)
        root = uf.find(n - 1)
        for i in range(0, n, 997):
            assert uf.find(i) == root
