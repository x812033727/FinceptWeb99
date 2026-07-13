from typing import Annotated

from fastapi import APIRouter, Depends

from auth.permissions import require_admin
from services.deploy_service import read_deploy_status, trigger_deploy

from ..schemas import DeployStatusOut, DeployTriggerOut

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]


@router.post("/deploy", response_model=DeployTriggerOut)
async def trigger_full_deploy(user: AdminUser) -> DeployTriggerOut:
    """Touch the host trigger file; the host's finceptweb-deploy.path
    systemd unit picks up the mtime change and runs the deploy script
    asynchronously. Returns immediately with a `trigger_id` the
    frontend uses to track this specific run via /deploy/status.
    """
    result = await trigger_deploy(actor_id=user["id"])
    return DeployTriggerOut(**result)


@router.get("/deploy/status", response_model=DeployStatusOut)
async def get_deploy_status(_: AdminUser) -> DeployStatusOut:
    """Return the current deploy phase + metadata.

    Polled every 2s by the frontend RedeployCard. Tolerates the host
    file being absent (returns `{phase: "idle"}`) and unparseable
    (returns `{phase: "unknown"}`) so the polling query never errors.
    """
    raw = await read_deploy_status()
    return DeployStatusOut(**raw)
