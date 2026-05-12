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

from app.db.database import get_db
from sqlalchemy.orm import Session
from sqlalchemy import text

@router.get("/profile")
async def get_profile(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.id:
        return {"details": None}
    
    # Query master_user_profiles
    res = db.execute(text("SELECT details FROM master_user_profiles WHERE user_id = :uid"), {"uid": user.id}).fetchone()
    
    # User's basic data from 'users' table
    user_res = db.execute(text("SELECT verified, original_uuid, department_id, manager_id, roles, status_id, level, grade FROM users WHERE id = :uid"), {"uid": user.id}).fetchone()
    
    profile_details = res[0] if res else {}
    
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "is_admin": user.is_admin,
        "details": profile_details,
        "extra": {
            "verified": user_res[0] if user_res else False,
            "roles": user_res[4] if user_res else [],
            "department_id": user_res[2] if user_res else None,
            "manager_id": user_res[3] if user_res else None,
            "status_id": user_res[5] if user_res else None,
            "level": user_res[6] if user_res else None,
            "grade": user_res[7] if user_res else None,
        }
    }
