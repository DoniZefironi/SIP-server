import re

_FIELDS = tuple(re.compile(pattern) for pattern in (
    r"\bаватар\w*\b|\bфот(?:о|ографи\w*) (?:в |для )?профил\w*\b",
    r"\b(?:имя|имени|фамили\w*|фио)\b",
    r"\b(?:парол\w*|пин[ -]?код\w*)\b",
    r"\b(?:электронн\w* почт\w*|e[ -]?mail|имейл\w*)\b",
    r"\b(?:номер\w* телефона|телефонн\w* номер\w*)\b",
    r"\b(?:язык\w*|тема|тему|темы) интерфейс\w*\b",
))
_CHANGE = re.compile(
    r"\b(?:поменять|сменить|изменить|обновить|поставить|установить|загрузить|добавить"
    r"|исправить|отредактировать|удалить|убрать|сбросить)\b"
)
_STEPS = re.compile(
    r"\b(?:поменя\w*|сме[нщ]\w*|измен\w*|обнов\w*|переимен\w*|установ\w*"
    r"|постав\w*|загруз\w*|добав\w*|исправ\w*|(?:от)?редактир\w*|введ\w*|укаж\w*"
    r"|удал\w*|убрать|уберите|сброс\w*|выберите|выбрать|нажмите|нажать"
    r"|сохраните|сохранить|нельзя|невозможно)\b"
)
_NO_ANSWER = re.compile(
    r"\b(?:нет (?:точного |подходящего )?(?:ответа|информации|сведений|инструкции)"
    r"|не (?:нашла|нашел|нашли|удалось найти) (?:точн\w+ |подходящ\w+ )?"
    r"(?:ответ\w*|информац\w*|сведени\w*|инструкци\w*)"
    r"|я не знаю|у меня нет данных|информаци\w* (?:нет|недостаточно))\b"
)


def is_relevant_answer(question: str, answer: str) -> bool:
    """Отсечь отказы и подмену поля настроек соседним."""
    ask = question.casefold().replace("ё", "е")
    reply = answer.casefold().replace("ё", "е")
    if _NO_ANSWER.search(reply):
        return False
    if not _CHANGE.search(ask):
        return True
    requested = [field for field in _FIELDS if field.search(ask)]
    return not requested or (
        all(field.search(reply) for field in requested) and bool(_STEPS.search(reply))
    )
