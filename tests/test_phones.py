import pytest

from app.services.phones import normalize_phone


@pytest.mark.parametrize(
    "raw",
    [
        "+375291234567",
        "375291234567",
        "80291234567",
        "8 (029) 123-45-67",
        "+375 29 123-45-67",
    ],
)
def test_belarusian_formats_give_one_number(raw: str) -> None:
    assert normalize_phone(raw) == "+375291234567"


def test_foreign_number_is_kept() -> None:
    assert normalize_phone("+74951234567") == "+74951234567"


@pytest.mark.parametrize("raw", [None, "", "unknown", "anonymous", "101", "12345"])
def test_hidden_or_internal_number_is_none(raw: str | None) -> None:
    assert normalize_phone(raw) is None
