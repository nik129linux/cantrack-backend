import pytest

from cantrack_api.structures import RatingWindow


class TestRatingWindow:
    def test_empty_window_has_no_average(self):
        rating_window = RatingWindow()
        assert rating_window.recent_average() is None
        assert rating_window.all_time_average() is None
        assert rating_window.recent_count() == 0

    def test_average_before_wrap(self):
        rating_window = RatingWindow(capacity=3)
        for rating in (5, 4, 3):
            rating_window.add(rating)
        assert rating_window.recent_average() == pytest.approx(4.0)

    def test_oldest_rating_drops_out_after_wrap(self):
        rating_window = RatingWindow(capacity=3)
        for rating in (1, 5, 5, 5):
            rating_window.add(rating)
        assert rating_window.recent_average() == pytest.approx(5.0)
        assert rating_window.recent_count() == 3

    def test_all_time_keeps_every_rating(self):
        rating_window = RatingWindow(capacity=3)
        for rating in (1, 5, 5, 5):
            rating_window.add(rating)
        assert rating_window.all_time_average() == pytest.approx(4.0)
        assert rating_window.all_time_count() == 4

    def test_default_capacity_is_25(self):
        rating_window = RatingWindow()
        for _ in range(26):
            rating_window.add(4)
        assert rating_window.recent_count() == 25

    def test_many_wraps_stay_exact(self):
        rating_window = RatingWindow(capacity=2)
        for rating in [1, 2, 3, 4, 5] * 40:
            rating_window.add(rating)
        assert rating_window.recent_average() == pytest.approx(4.5)

    @pytest.mark.parametrize("bad_rating", [0, 0.99, 5.01, 6, -1])
    def test_out_of_range_rating_is_rejected(self, bad_rating):
        rating_window = RatingWindow()
        with pytest.raises(ValueError):
            rating_window.add(bad_rating)
        assert rating_window.all_time_count() == 0
        assert rating_window.recent_count() == 0
        assert rating_window.recent_average() is None

    def test_rejected_rating_leaves_existing_window_intact(self):
        rating_window = RatingWindow(capacity=3)
        rating_window.add(4)
        with pytest.raises(ValueError):
            rating_window.add(9)
        assert rating_window.recent_average() == pytest.approx(4.0)
        assert rating_window.recent_count() == 1

    @pytest.mark.parametrize("bad_capacity", [0, -3])
    def test_capacity_must_be_positive(self, bad_capacity):
        with pytest.raises(ValueError):
            RatingWindow(capacity=bad_capacity)
