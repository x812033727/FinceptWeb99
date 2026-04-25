from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from dependencies import get_current_user
from services.version_service import get_version_status

router = APIRouter()
User = Annotated[dict, Depends(get_current_user)]


class VersionStatus(BaseModel):
    current: str
    latest: str
    update_available: bool
    html_url: str
    published_at: str


@router.get("/version", response_model=VersionStatus)
async def version(_: User) -> VersionStatus:
    data = await get_version_status()
    return VersionStatus(**data)
