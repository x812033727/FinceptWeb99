from fastapi import Depends, HTTPException, status
from dependencies import get_current_user


def require_role(*roles: str):
    """Factory: returns a dependency that enforces the given roles."""
    async def _check(user: dict = Depends(get_current_user)):
        if user["role"] not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user
    return _check


require_viewer  = require_role("viewer", "analyst", "admin")
require_analyst = require_role("analyst", "admin")
require_admin   = require_role("admin")
