import re
from typing import Literal

type SafetyRoute = Literal["emergency", "urgent_operator"]

_CLAUSE = re.compile(r"[.!?;]+|\b(?:но|однако|зато)\b")
_GLOW = r"(?:горит|горят|загорел(?:ся|ась|ось|ись))"
_INDICATOR = r"(?:индикатор\w*|светодиод\w*|лампочк\w*|пиктограмм\w*|надпись)"
_COLOR = r"(?:красн\w*|зелен\w*|желт\w*|оранжев\w*|син\w*|бел\w*|аварийн\w*)"
_INDICATOR_LABEL = r"(?:питания|связи|ошибки|сети|работы|аварии|на (?:панели|шкафу|экране))"
_INDICATOR_GLOW = re.compile(
    rf"\b(?:{_GLOW}\s+(?:{_COLOR}\s+){{0,2}}{_INDICATOR}"
    rf"|{_INDICATOR}(?:\s+{_INDICATOR_LABEL})?\s+{_GLOW})\b"
)
_DANGER = re.compile(
    rf"\b(?:{_GLOW}|загорел[а-я]+|горени[ея]|тлеет|тлеют|пламен[яий]|огонь|огня"
    r"|пожар(?:а|е|ом)?|возгорани[ея]|воспламенил[а-я]+"
    r"|дым(?:а|ом|у|ится|ятся|ит|ят)?|задымл[а-я]+|задымил[а-я]+"
    r"|взрыв(?:а|е|ом)?|взорвал[а-я]+|искрит|искрят|(?:летят|сыпятся|пошли) искры"
    r"|сработал[аи]? (?:датчик дыма|пожарная сигнализация)"
    r"|утечк[аиу] газа|пахнет газом"
    r"|(?:ударил[оаи]?|бьет|бьют|шарахнул[оаи]?|убило) (?:током|электричеством)"
    r"|поражение (?:электрическим )?током|сильн[а-я]+ (?:кровотечени\w*|ожог\w*)"
    r"|человек под напряжением|(?:человек|сотрудник|коллега) не дышит"
    r"|потерял[аи]? сознание|без сознания|не могу дышать|угроз[ау] жизни)\b"
)
_NO_DANGER_BEFORE = re.compile(
    r"\b(?:нет|без|не|ни|никакого|никакой|отсутствует)\s+(?:(?:ни|следов|признаков)\s+)?$"
)
_NO_DANGER_AFTER = re.compile(r"^\s+(?:(?:уже|больше)\s+)?(?:нет|не было|отсутствует)\b")
_HYPOTHETICAL = re.compile(
    r"\b(?:на случай|в случае|учебн\w*|тренировочн\w*|предположим|в теории"
    r"|инструкци\w*|памятк\w*|регламент\w*|сигнал|режим)\b"
)
_PAST = re.compile(r"\b(?:вчера|на прошлой неделе|потушил[аи]?|ликвидировал[аи]?)\b")
_CURRENT = re.compile(r"\b(?:сейчас|сегодня|теперь|снова|у нас|срочно|помогите)\b")
_TECHNICAL_RISK = re.compile(
    r"\b(?:(?:пахнет|воняет) (?:гарью|горелым|паленым|горелой проводкой|жженой проводкой)"
    r"|запах (?:гари|горелого)"
    r"|перегрел[а-я]+|перегрев(?:а|ом)?|сильно гре[ею]тся|расплавил[а-я]+|оплавил[а-я]+"
    r"|короткое замыкание|коротнул[а-я]*|замкнул[а-я]+|вышел из строя"
    r"|под напряжением|оголен[а-я]* провод[а-я]*|пробой на корпус"
    r"|залило (?:шкаф|оборудовани\w*)|(?:вода|жидкость) (?:в|попала в) (?:шкаф|щит)"
    r"|(?:выбил[оаи]?|выбивает) автомат|срабатывает защита"
    r"|отключить защиту|обойти защиту|закоротить|зашунтировать|перемкнуть контакты"
    r"|срочно (?:помогите|нужна помощь)"
    r"|аварийн[а-я]+ (?:отключени\w*|останов\w*|ситуаци\w*)|авария)\b"
)
_REPAIR_ADVICE = re.compile(
    r"\b(?:снимите (?:крышку|кожух)|откройте дверцу (?:шкафа|щита)"
    r"|перемкните|замкните|закоротите|зашунтируйте"
    r"|(?:отсоедините|переподключите|поменяйте) (?:провод[а-я]*|кабел[а-я]*)"
    r"|(?:отключите|обойдите) защиту)\b"
)
_CABINET_CONNECTION = re.compile(
    r"(?=[^.!?]*\b(?:шкаф\w*|щит\w*)\b)[^.!?]*\b(?:как|куда|к чему)\b"
    r"[^.!?]*\b(?:подключ\w*|подсоедин\w*)\b", re.IGNORECASE,
)
_DOCUMENT_QUESTION = re.compile(
    r"\b(?:где|найти|открыть|скачать|посмотреть|приложени\w*)\b"
    r"[^.!?]*\b(?:схем\w*|инструкци\w*|документ\w*|приложени\w*)\b"
    r"|\b(?:в|к)\s+приложени\w*\b|\bSavt\s+Assist\b",
    re.IGNORECASE,
)


def safety_route(text: str) -> SafetyRoute | None:
    """Направить опасную реплику мимо базы знаний."""
    clauses = _CLAUSE.split(text.casefold().replace("ё", "е"))
    urgent = False
    for clause in clauses:
        clause = _INDICATOR_GLOW.sub(" ", clause)
        for match in _DANGER.finditer(clause):
            before, after = clause[:match.start()], clause[match.end():]
            if _NO_DANGER_BEFORE.search(before) or _NO_DANGER_AFTER.match(after):
                continue
            if (_HYPOTHETICAL.search(before) or _PAST.search(clause)) and not _CURRENT.search(
                clause
            ):
                urgent = True
            else:
                return "emergency"
        urgent |= bool(_TECHNICAL_RISK.search(clause))
    return "urgent_operator" if urgent else None


def answer_requires_operator(text: str) -> bool:
    return safety_route(text) is not None or bool(_REPAIR_ADVICE.search(text.casefold()))


def requires_specialist(text: str) -> bool:
    return bool(_CABINET_CONNECTION.search(text) and not _DOCUMENT_QUESTION.search(text))
