from cantrack_api.bidding import DEFAULT_CONFIG, WalkOffer, rank_offers, score_offers


def make_offer(offer_id, price_cop=25000, distance_km=1.0, recent_rating=None,
               bucket_empty=False, created_at=1000.0, walker_id="walker-w"):
    return WalkOffer(offer_id=offer_id, walker_id=walker_id, price_cop=price_cop,
                     distance_km=distance_km, recent_rating=recent_rating,
                     bucket_empty=bucket_empty, created_at=created_at)


class TestBiddingScoring:
    def test_config_defaults_match_the_spec(self):
        assert (DEFAULT_CONFIG.radius_km, DEFAULT_CONFIG.detour_limit_min,
                DEFAULT_CONFIG.auto_pick_after_min, DEFAULT_CONFIG.walker_capacity) == (2.0, 10.0, 10.0, 4)
        assert (DEFAULT_CONFIG.price_weight, DEFAULT_CONFIG.distance_weight,
                DEFAULT_CONFIG.rating_weight, DEFAULT_CONFIG.empty_bucket_penalty) == (0.4, 0.3, 0.3, 0.8)
        assert (DEFAULT_CONFIG.cancel_tokens_per_week, DEFAULT_CONFIG.no_show_grace_min) == (3, 15.0)

    def test_single_offer_scores_one(self):
        assert score_offers([make_offer("only")]) == {"only": 1.0}

    def test_cheaper_closer_better_rated_wins(self):
        weak = make_offer("weak", price_cop=40000, distance_km=2.0, recent_rating=2.0, created_at=1000.0)
        strong = make_offer("strong", price_cop=20000, distance_km=0.5, recent_rating=5.0, created_at=2000.0)
        assert rank_offers([weak, strong]) == ["strong", "weak"]

    def test_min_max_normalisation(self):
        cheap = make_offer("cheap", price_cop=10000, created_at=1000.0)
        dear = make_offer("dear", price_cop=30000, created_at=2000.0)
        scores = score_offers([cheap, dear])
        assert scores["cheap"] > scores["dear"]

    def test_earliest_offer_breaks_ties(self):
        first = make_offer("first", created_at=1000.0)
        second = make_offer("second", created_at=2000.0)
        assert rank_offers([second, first]) == ["first", "second"]

    def test_empty_bucket_multiplies_by_penalty(self):
        penalised = make_offer("penalised", bucket_empty=True, created_at=1000.0)
        clean = make_offer("clean", bucket_empty=False, created_at=2000.0)
        scores = score_offers([penalised, clean])
        assert scores["penalised"] == scores["clean"] * 0.8

    def test_unrated_walker_scores_zero_on_rating(self):
        unrated = make_offer("unrated", recent_rating=None, created_at=1000.0)
        rated = make_offer("rated", recent_rating=5.0, created_at=2000.0)
        assert score_offers([unrated, rated])["unrated"] < score_offers([unrated, rated])["rated"]

    def test_empty_list_scores_empty(self):
        assert score_offers([]) == {}
        assert rank_offers([]) == []
