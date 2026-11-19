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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Форматы, встреченные в CRM и карточках сотрудников Bitrix SAVT.
        ("8 (0222) 62-95-37", "+375222629537"),
        ("+37525 645 21 12", "+375256452112"),
        ("+375(29)7437366", "+375297437366"),
        ("   +375259016756", "+375259016756"),
    ],
)
def test_formats_seen_in_bitrix(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["300059047", "193200595"])
def test_tax_ids_in_phone_field_are_not_numbers(raw: str) -> None:
    """В поле телефона у некоторых компаний записан УНП, номером он не считается."""
    assert normalize_phone(raw) is None


def test_foreign_number_is_kept() -> None:
    assert normalize_phone("+74951234567") == "+74951234567"


@pytest.mark.parametrize("raw", [None, "", "unknown", "anonymous", "101", "12345"])
def test_hidden_or_internal_number_is_none(raw: str | None) -> None:
    assert normalize_phone(raw) is None
