from fastapi import APIRouter, Depends

from app.core.security import CurrentUser, get_current_user


router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@router.get("/me")
async def get_me(user: CurrentUser = Depends(get_current_user)):
    return {
        "id": user.id,
        "pocketbase_id": user.pocketbase_id,
        "email": user.email,
        "name": user.name,
        "is_admin": user.is_admin,
        "is_service_account": user.is_service_account,
    }
