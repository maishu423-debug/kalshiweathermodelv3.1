"""Daily-high PDF: path-max preferred over the (warm-biased) product of horizon CDFs."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_api import algorithm as alg  # noqa: E402


def _model(high_temps, high_probs):
    return {"temps": high_temps, "probs": high_probs, "high_temps": high_temps, "high_probs": high_probs}


def test_path_high_pdf_mixes_3h_pair_with_weights_and_normalises():
    results = {
        "accuweather_3h": _model([88, 89], [0.5, 0.5]),
        "var_3h": _model([89, 90], [1.0, 0.0]),
    }
    pdf = alg._get_path_high_pdf(results)
    assert sum(pdf.values()) == pytest.approx(1.0)
    wa, wv = alg.HORIZON_WEIGHTS["3h"]["accuweather_3h"], alg.HORIZON_WEIGHTS["3h"]["var_3h"]
    assert pdf[88] == pytest.approx(wa * 0.5 / (wa + wv))
    assert pdf[89] == pytest.approx((wa * 0.5 + wv * 1.0) / (wa + wv))


def test_path_high_pdf_uses_all_six_models():
    # each model is a point mass on a distinct temperature -> output weight == its effective weight
    temps = {"accuweather_1h": 80, "var_1h": 81, "accuweather_2h": 82, "var_2h": 83,
             "accuweather_3h": 84, "var_3h": 85}
    results = {name: _model([t], [1.0]) for name, t in temps.items()}
    pdf = alg._get_path_high_pdf(results)
    assert set(pdf) == set(temps.values())
    assert sum(pdf.values()) == pytest.approx(1.0)
    total = sum(alg.HORIZON_MIX[h] * w for h, pair in alg.HORIZON_WEIGHTS.items() for w in pair.values())
    for h, pair in alg.HORIZON_WEIGHTS.items():
        for name, w in pair.items():
            assert pdf[temps[name]] == pytest.approx(alg.HORIZON_MIX[h] * w / total)


def test_path_high_pdf_renormalises_when_a_model_is_missing():
    results = {
        "accuweather_1h": _model([80], [1.0]),
        "accuweather_3h": _model([84], [1.0]),
        "var_2h": {"error": "boom"},
    }
    pdf = alg._get_path_high_pdf(results)
    assert sum(pdf.values()) == pytest.approx(1.0)
    w1 = alg.HORIZON_MIX["1h"] * alg.HORIZON_WEIGHTS["1h"]["accuweather_1h"]
    w3 = alg.HORIZON_MIX["3h"] * alg.HORIZON_WEIGHTS["3h"]["accuweather_3h"]
    assert pdf[80] == pytest.approx(w1 / (w1 + w3))


def test_path_high_pdf_empty_when_models_lack_it_or_failed():
    assert alg._get_path_high_pdf({"accuweather_3h": {"temps": [88], "probs": [1.0]}}) == {}
    assert alg._get_path_high_pdf({"accuweather_3h": {"error": "boom"}, "var_3h": {"error": "boom"}}) == {}


def test_high_cannot_be_below_the_observed_high():
    pdf = alg._compute_daily_high_pdf({}, 90.2, {86: 0.2, 90: 0.5, 92: 0.3})
    assert min(pdf) == 90                       # 86 mass folded onto the rounded observed high
    assert pdf[90] == pytest.approx(0.7)
    assert pdf[92] == pytest.approx(0.3)
    assert sum(pdf.values()) == pytest.approx(1.0)


def test_falls_back_to_cdf_product_without_path_pdf():
    horizon = {"1h": {88: 0.5, 89: 0.5}, "2h": {88: 0.5, 89: 0.5}}
    pdf = alg._compute_daily_high_pdf(horizon, 88.0)
    # independent draws: P(max<=88)=0.25 -> the independence assumption is what makes this warm-biased
    assert pdf[88] == pytest.approx(0.25)
    assert pdf[89] == pytest.approx(0.75)
