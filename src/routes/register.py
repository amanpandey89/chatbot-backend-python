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


@router.post("/register")
def register_store(body: RegisterRequest):
    store_id = str(uuid.uuid4())

    register_tenant(
        store_id,
        {
            "store_url": body.store_url.rstrip("/"),
            "consumer_key": body.consumer_key,
            "consumer_secret": body.consumer_secret,
            "store_name": body.store_name,
        },
    )

    return {
        "success": True,
        "store_id": store_id,
        "embed_snippet": f"<script src='http://localhost:8000/static/widget.js' data-store-id='{store_id}'></script>",
    }
