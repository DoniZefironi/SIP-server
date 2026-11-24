from app.ami.client import AmiClient, AmiError, Message


class AstDB:
    def __init__(self, ami: AmiClient) -> None:
        self._ami = ami

    async def get(self, family: str, key: str) -> str | None:
        self._require_connection()
        reply, events = await self._ami.list_action(
            {"Action": "DBGet", "Family": family, "Key": key}
        )
        if reply.get("Response") == "Error" and reply.get("Message") == "Database entry not found":
            return None
        self._check(reply, "чтение значения AstDB")
        for event in events:
            if event.get("Event") == "DBGetResponse" and "Val" in event:
                return event["Val"]
        raise AmiError("AMI не вернул значение AstDB")

    async def list_family(self, family: str) -> dict[str, str]:
        self._require_connection()
        reply, events = await self._ami.list_action({"Action": "DBGetTree", "Family": family})
        self._check(reply, "чтение раздела AstDB")
        prefix = f"/{family}/"
        return {
            event["Key"].removeprefix(prefix): event["Val"]
            for event in events
            if event.get("Event") == "DBGetTreeResponse"
            and event.get("Key", "").startswith(prefix)
            and "Val" in event
        }

    async def put(self, family: str, key: str, value: str) -> None:
        self._require_connection()
        reply = await self._ami.send_action(
            {"Action": "DBPut", "Family": family, "Key": key, "Val": value}
        )
        self._check(reply, "сохранение в AstDB")

    async def delete(self, family: str, key: str) -> None:
        self._require_connection()
        reply = await self._ami.send_action({"Action": "DBDel", "Family": family, "Key": key})
        if reply.get("Message") == "Database entry could not be deleted":
            if await self.get(family, key) is None:
                return
        self._check(reply, "удаление из AstDB")

    def _require_connection(self) -> None:
        if not self._ami.connected:
            raise AmiError("Нет соединения с AMI")

    @staticmethod
    def _check(reply: Message, operation: str) -> None:
        if reply.get("Response") != "Success":
            raise AmiError(f"AMI отклонил {operation}")
