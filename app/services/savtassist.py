import asyncio
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

type ObjectKind = Literal["project", "cabinet"]


class ObjectLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    number: str = Field(min_length=1, max_length=100, strict=True)
    kind: ObjectKind | None = None


class ObjectMatch(BaseModel):
    kind: ObjectKind
    cabinet_id: int | None
    cabinet_number: str | None
    project_id: int | None
    project_number: str | None
    bitrix_deal_id: str | None


class ObjectLookupResult(BaseModel):
    status: Literal["found", "ambiguous", "not_found"]
    match: ObjectMatch | None = None

    @model_validator(mode="after")
    def validate_match(self) -> "ObjectLookupResult":
        if (self.status == "found") != (self.match is not None):
            raise ValueError("Результат поиска не соответствует статусу")
        return self


class CallObjectLookup(ObjectLookupResult):
    number: str


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000, strict=True)


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    status: Literal["answered", "needs_operator"]
    answer: str | None = Field(default=None, min_length=1, max_length=600)

    @model_validator(mode="after")
    def validate_answer(self) -> "QuestionAnswer":
        if (self.status == "answered") != (self.answer is not None):
            raise ValueError("Ответ на вопрос не соответствует статусу")
        return self


class SavtAssistError(Exception):
    pass


class SavtAssist:
    def __init__(self, http: httpx.AsyncClient, base_url: str, secret: str) -> None:
        url = httpx.URL(base_url.strip())
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("Нужен HTTPS-адрес SavtAssist без пути или параметров")
        self._url = url
        self._http = http
        self._secret = secret

    async def _post(self, endpoint: str, request: BaseModel, timeout: float) -> httpx.Response:
        try:
            async with asyncio.timeout(timeout):
                response = await self._http.post(
                    self._url.copy_with(path=f"/internal/telephony/{endpoint}"),
                    headers={"X-Telephony-Secret": self._secret},
                    json=request.model_dump(exclude_none=True),
                    follow_redirects=False,
                    timeout=timeout,
                )
            response.raise_for_status()
        except (httpx.HTTPError, TimeoutError):
            raise SavtAssistError("SavtAssist не подтвердил запрос") from None
        return response

    async def resolve_object(self, request: ObjectLookupRequest) -> CallObjectLookup:
        response = await self._post("resolve-object", request, timeout=4)
        try:
            result = ObjectLookupResult.model_validate_json(response.content)
        except ValidationError:
            raise SavtAssistError("SavtAssist вернул неверный результат поиска") from None
        return CallObjectLookup(number=request.number, **result.model_dump())

    async def answer_question(self, request: QuestionRequest) -> QuestionAnswer:
        response = await self._post("answer-question", request, timeout=10)
        try:
            return QuestionAnswer.model_validate_json(response.content)
        except ValidationError:
            raise SavtAssistError("SavtAssist вернул неверный ответ на вопрос") from None
