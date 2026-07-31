import pytest

from app.mathutil import normalize_percent


def test_normalize_percent():
    assert normalize_percent(12.5) == 0.125


def test_negative_rejected():
    with pytest.raises(ValueError):
        normalize_percent(-1)
