import phonenumbers

DEFAULT_REGION = "BY"


def normalize_phone(raw: str | None, region: str = DEFAULT_REGION) -> str | None:
    """Привести номер к виду +375291234567 (E.164).

    Понимает «+375…», «375…», «80…» и номера с пробелами, скобками, дефисами.
    Возвращает None, если номер скрыт или это не телефонный номер: для звонка это не ошибка.
    """
    if not raw:
        return None
    candidate = raw.strip()
    digits = "".join(ch for ch in candidate if ch.isdigit())
    # «375…» без плюса библиотека приняла бы за местный номер, поэтому плюс дописываем сами.
    if not candidate.startswith("+") and digits.startswith("375") and len(digits) == 12:
        candidate = "+" + digits
    try:
        parsed = phonenumbers.parse(candidate, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def crm_phone_variants(number: str | None, raw: str | None) -> list[str]:
    """Форматы, в которых полный номер может быть сохранён в карточке CRM."""
    if not number:
        return []
    digits = number.removeprefix("+")
    variants = [number, digits, raw or number]
    if len(digits) == 12 and digits.startswith("375"):
        area, first, middle, last = digits[3:5], digits[5:8], digits[8:10], digits[10:]
        variants.extend((
            f"80{digits[3:]}",
            f"+375 ({area}) {first}-{middle}-{last}",
            f"+375({area}){first}{middle}{last}",
        ))
    return list(dict.fromkeys(variants))
