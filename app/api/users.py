from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import PremiumUpdate, UserRead

router = APIRouter(prefix="/users", tags=["users"])


@router.patch("/me/premium", response_model=UserRead)
def update_premium_status(
    payload: PremiumUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    current_user.is_premium = payload.is_premium
    db.commit()
    db.refresh(current_user)
    return current_user
