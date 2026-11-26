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
    r"|я не знаю|не знаю,? (?:что ответить|как ответить|чем помочь)"
    r"|не могу (?:точно )?ответить|у меня нет данных|информаци\w* (?:нет|недостаточно))\b"
)
_CHAT = re.compile(r"\b(?:чат(?:а|у|е|ом|ы|ов|ам|ах|ами)?|мессенджер\w*|переписк\w*)\b")
_CHAT_CLARIFICATION = re.compile(
    r"(?:а )?(?:что (?:такое|значит) чат|что это за чат|что за чат|какой чат"
    r"|(?:в|о) каком чате(?: (?:речь|вы говорите))?|в какой чат(?: мне)? писать)"
)
_EQUIPMENT_FAULT = re.compile(
    r"\b(?:ошибк\w*|сбой|неисправност\w*|не работает|не включается|не запускается)\b"
)
_PROFILE_SETTINGS = re.compile(r"\b(?:профил\w*|аватар\w*|личн\w* кабинет\w*)\b")


def phone_channel_answer(question: str) -> str | None:
    words = " ".join(re.findall(r"[^\W_]+", question.casefold()))
    if _CHAT_CLARIFICATION.fullmatch(words):
        return (
            "Чат — это переписка. Сейчас мы разговариваем по телефону. "
            "Если у вас неполадка, расскажите о ней голосом."
        )
    return None


def is_relevant_answer(question: str, answer: str) -> bool:
    """Отсечь отказы, непрошеный переход в чат и подмену поля настроек."""
    ask = question.casefold().replace("ё", "е")
    reply = answer.casefold().replace("ё", "е")
    if _NO_ANSWER.search(reply) or (_CHAT.search(reply) and not _CHAT.search(ask)):
        return False
    if _EQUIPMENT_FAULT.search(ask) and not _PROFILE_SETTINGS.search(ask):
        if _PROFILE_SETTINGS.search(reply) and not _EQUIPMENT_FAULT.search(reply):
            return False
    if not _CHANGE.search(ask):
        return True
    requested = [field for field in _FIELDS if field.search(ask)]
    return not requested or (
        all(field.search(reply) for field in requested) and bool(_STEPS.search(reply))
    )
