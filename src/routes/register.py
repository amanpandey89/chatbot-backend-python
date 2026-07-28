import uuid
from fastapi import APIRouter
from pydantic import BaseModel
from src.services.store import register_tenant

router = APIRouter()


class RegisterRequest(BaseModel):
    store_url: str
    consumer_key: str
    consumer_secret: str
    store_name: str = "My Store"
    platform: str = "woocommerce"


@router.post("/register")
def register_store(body: RegisterRequest):
    store_id = str(uuid.uuid4())
    platform = (body.platform or "woocommerce").strip().lower()
    if platform == "wordpress":
        platform = "woocommerce"

    register_tenant(
        store_id,
        {
            "platform": platform,
            "store_url": body.store_url.rstrip("/"),
            "consumer_key": body.consumer_key,
            "consumer_secret": body.consumer_secret,
            "store_name": body.store_name,
        },
    )

    return {
        "success": True,
        "store_id": store_id,
        "platform": platform,
        "embed_snippet": (
            f"<script src='/static/chatbot.js' data-store-id='{store_id}' "
            f"data-backend-url='' defer></script>"
        ),
    }
