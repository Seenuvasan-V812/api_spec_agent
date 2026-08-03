from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/token",
    scopes={"pets:read": "Read pets", "pets:write": "Modify pets"},
)


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    return {"token": token}
