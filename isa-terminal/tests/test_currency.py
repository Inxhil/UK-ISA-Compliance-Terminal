"""GBp/GBP scaling and FX conversion — the 100x-error guard rails (CLAUDE.md section 2)."""
import math

import pytest

from engine.currency import (fx_symbol, normalise_price, price_to_gbp, reconcile_unit, split_currency, to_gbp)


class TestPenceVsPounds:
    def test_gbp_pence_is_divided_by_100(self):
        assert normalise_price(13628.0, "GBp") == pytest.approx(136.28)

    def test_gbx_alias_is_pence(self):
        assert normalise_price(1450.5, "GBX") == pytest.approx(14.505)

    def test_gbp_pounds_untouched(self):
        assert normalise_price(138.30, "GBP") == pytest.approx(138.30)

    def test_case_sensitivity_is_the_whole_point(self):
        # 'GBp' and 'GBP' differ ONLY by case; collapsing them is the classic 100x bug.
        assert normalise_price(100, "GBp") == 1.0
        assert normalise_price(100, "GBP") == 100.0
        assert split_currency("GBp") == ("GBP", 100.0)
        assert split_currency("GBP") == ("GBP", 1.0)

    def test_does_not_infer_pence_from_dot_L_suffix(self):
        # VWRP.L is quoted in pounds even though it is an LSE ticker. Only the currency decides.
        assert normalise_price(138.3, "GBP") == pytest.approx(138.3)

    def test_override_forces_unit(self):
        # Vendor mislabels a pounds-quoted fund as GBp -> config price_unit: GBP fixes it
        assert normalise_price(120.0, "GBp", override="GBP") == pytest.approx(120.0)
        assert normalise_price(12000.0, "GBP", override="GBp") == pytest.approx(120.0)
        assert normalise_price(12000.0, "GBp", override="auto") == pytest.approx(120.0)

    def test_index_points_never_converted(self):
        assert normalise_price(10585.5, "GBP", is_index=True) == 10585.5
        assert normalise_price(10585.5, "GBp", is_index=True) == 10585.5

    @pytest.mark.parametrize("bad", [None, float("nan"), "abc"])
    def test_bad_input_returns_none(self, bad):
        assert normalise_price(bad, "GBp") is None

    def test_missing_currency_assumes_major_units(self):
        assert normalise_price(5.0, None) == 5.0

    def test_other_subunits(self):
        assert split_currency("ZAc") == ("ZAR", 100.0)


class TestFx:
    FX = {"USD": 1.3154, "EUR": 1.15}

    def test_gbpusd_is_usd_per_gbp_so_divide(self):
        # $501.69 at 1.3154 USD/GBP = ~£381.40  (NOT multiplied!)
        assert to_gbp(501.69, "USD", self.FX) == pytest.approx(381.40, abs=0.01)

    def test_gbp_passthrough_needs_no_rate(self):
        assert to_gbp(10.0, "GBP", {}) == 10.0

    def test_missing_rate_returns_none_not_a_guess(self):
        assert to_gbp(10.0, "USD", {}) is None
        assert to_gbp(10.0, "JPY", self.FX) is None

    @pytest.mark.parametrize("rate", [0, -1.0])
    def test_non_positive_rate_rejected(self, rate):
        assert to_gbp(10.0, "USD", {"USD": rate}) is None

    def test_price_to_gbp_pence_stock(self):
        assert price_to_gbp(13628.0, "GBp", self.FX) == pytest.approx(136.28)

    def test_price_to_gbp_usd_stock(self):
        assert price_to_gbp(152.185, "USD", self.FX) == pytest.approx(152.185 / 1.3154)

    def test_price_to_gbp_usd_without_fx_is_none(self):
        assert price_to_gbp(152.185, "USD", {}) is None

    def test_price_to_gbp_index_passthrough(self):
        assert price_to_gbp(7308.9, "USD", {}, is_index=True) == pytest.approx(7308.9)

    def test_fx_symbol(self):
        assert fx_symbol("USD") == "GBPUSD=X"
        assert fx_symbol("EUR") == "GBPEUR=X"
        assert fx_symbol("GBP") is None


class TestReconcileUnit:
    def test_target_in_pence_vs_price_in_pounds(self):
        v, note = reconcile_unit(15000.0, 136.28)       # target 15,000 (pence) vs £136.28
        assert v == pytest.approx(150.0) and "corrected" in note

    def test_target_in_pounds_vs_price_in_pence(self):
        v, note = reconcile_unit(150.0, 13628.0)
        assert v == pytest.approx(15000.0) and note

    def test_sane_ratio_untouched(self):
        assert reconcile_unit(636.1, 501.4) == (636.1, None)

    def test_none_and_zero_reference(self):
        assert reconcile_unit(None, 10) == (None, None)
        assert reconcile_unit(10, 0) == (10, None)

    def test_does_not_over_correct_wild_values(self):
        v, note = reconcile_unit(1_000_000.0, 10.0)      # ratio 1e5: /100 still absurd -> leave
        assert v == 1_000_000.0 and note is None
