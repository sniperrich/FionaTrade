from __future__ import annotations

from scripts.relabel_routine_filings import _is_routine_filing_summary


def test_relabel_detects_routine_filing_summary():
    assert _is_routine_filing_summary("AAPL filed 8-K") is True
    assert _is_routine_filing_summary("MSFT filed Form 10-Q") is True


def test_relabel_ignores_material_filing_summary():
    assert _is_routine_filing_summary("AAPL filed 8-K disclosing material weakness") is False
