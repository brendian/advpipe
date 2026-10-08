import pytest

from mathutils import mean, safe_div


def test_safe_div() -> None:
    assert safe_div(6, 3) == 2


def test_safe_div_by_zero_returns_default() -> None:
    assert safe_div(1, 0, default=-1.0) == -1.0


def test_mean() -> None:
    assert mean([1, 2, 3]) == 2


def test_mean_empty_raises() -> None:
    with pytest.raises(ValueError, match="values"):
        mean([])
