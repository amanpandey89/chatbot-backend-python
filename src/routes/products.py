from fastapi import APIRouter, HTTPException
from src.services.store import get_tenant
from src.services.catalog import fetch_products

router = APIRouter()


@router.get("/products")
async def get_products(store_id: str):
    tenant = get_tenant(store_id)

    if not tenant:
        raise HTTPException(
            status_code=404, detail="Store not found. Please register first."
        )

    try:
        products = await fetch_products(tenant)
        return {"success": True, "count": len(products), "products": products}

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch products: {str(e)}"
        )
