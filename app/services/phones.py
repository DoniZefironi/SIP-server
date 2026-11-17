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
