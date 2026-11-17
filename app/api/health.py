from fastapi import APIRouter, Request, Response, status

from app.db.session import database_is_up

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, str]:
    """Состояние сервиса: 200, если база и связь со станцией в порядке, иначе 503."""
    db = "ok" if await database_is_up() else "down"
    client = request.app.state.ami
    if client is None:
        ami = "disabled"
    else:
        ami = "connected" if client.connected else "disconnected"

    healthy = db == "ok" and ami != "disconnected"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "db": db, "ami": ami}
