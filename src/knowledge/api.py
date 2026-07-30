"""
REST API for tenant RAG knowledge management.
WordPress + Shopify use the same endpoints with X-Store-Id / X-Tenant-Key.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from src.services.store import get_tenant
from src.routes.tenant_api import _auth_store
from src.knowledge import (
    SOURCE_TYPES,
    get_rag_settings,
    update_rag_settings,
    overview_stats,
    list_logs,
    log_event,
)
from src.knowledge.ingest import (
    list_sources,
    delete_source,
    upsert_and_index_async_prep,
    index_source_async,
)
from src.knowledge.exclusions import list_exclusions, add_exclusion, delete_exclusion
from src.knowledge.retrieve import search
from src.knowledge.sync import create_job, get_job, list_jobs, run_job, rebuild_embeddings
from src.knowledge.connectors.shopify_sync import collect_shopify_knowledge
from src.knowledge.connectors.woocommerce_sync import (
    collect_woocommerce_knowledge,
    woocommerce_sync_ready,
)
from src.knowledge.connectors.crawler import crawl_urls
from src.services import training as training_svc

router = APIRouter(prefix="/tenant", tags=["knowledge-rag"])


class RagSettingsBody(BaseModel):
    top_k: Optional[int] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    enabled_sources: Optional[List[str]] = None
    crawler_seed_urls: Optional[str] = None


class KnowledgeItem(BaseModel):
    source_type: str = "custom"
    external_id: str
    title: str
    url: str = ""
    body: str = ""
    html: str = ""
    meta: Optional[Dict[str, Any]] = None


class BulkIngestBody(BaseModel):
    items: List[KnowledgeItem] = Field(default_factory=list)
    run_async: bool = True


class FaqBody(BaseModel):
    title: str
    content: str
    external_id: Optional[str] = None


class CustomBody(BaseModel):
    title: str
    content: str
    external_id: Optional[str] = None


class ExclusionBody(BaseModel):
    match_type: str = Field(..., pattern="^(url|id|path|title)$")
    match_value: str
    reason: str = ""


class SearchTestBody(BaseModel):
    query: str
    top_k: Optional[int] = None


class SyncJobBody(BaseModel):
    job_type: str = "incremental_sync"
    items: Optional[List[KnowledgeItem]] = None
    seed_urls: Optional[List[str]] = None


def _tenant_or_404(store_id: str):
    t = get_tenant(store_id)
    if not t:
        raise HTTPException(status_code=404, detail="Store not found")
    return {**t, "store_id": store_id}


@router.get("/{store_id}/rag/overview")
def rag_overview(request: Request, store_id: str):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    return {
        "success": True,
        "overview": overview_stats(store_id),
        "settings": get_rag_settings(store_id),
        "source_types": list(SOURCE_TYPES),
        "legacy_training": training_svc.get_tenant_settings(store_id),
    }


@router.get("/{store_id}/rag/settings")
def rag_get_settings(request: Request, store_id: str):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    return {"success": True, "settings": get_rag_settings(store_id)}


@router.patch("/{store_id}/rag/settings")
def rag_patch_settings(request: Request, store_id: str, body: RagSettingsBody):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    fields = body.model_dump(exclude_none=True)
    settings = update_rag_settings(store_id, **fields)
    return {"success": True, "settings": settings}


@router.get("/{store_id}/rag/sources")
def rag_list_sources(
    request: Request, store_id: str, source_type: Optional[str] = None
):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    items = list_sources(store_id, source_type=source_type)
    return {"success": True, "count": len(items), "sources": items}


@router.delete("/{store_id}/rag/sources/{source_id}")
def rag_delete_source(request: Request, store_id: str, source_id: str):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    ok = delete_source(store_id, source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"success": True}


@router.post("/{store_id}/rag/ingest")
async def rag_ingest(
    request: Request,
    store_id: str,
    body: BulkIngestBody,
    background_tasks: BackgroundTasks,
):
    """WordPress / integrations push content here for indexing."""
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    items = [i.model_dump() for i in body.items]
    if not items:
        raise HTTPException(status_code=400, detail="No items")

    job = create_job(store_id, "content_ingest", {"count": len(items)})

    async def _run():
        await run_job(store_id, job["id"], items)

    if body.run_async:
        background_tasks.add_task(_run)
        return {"success": True, "job": job, "queued": len(items)}

    result = await run_job(store_id, job["id"], items)
    return {"success": True, "job": result}


@router.post("/{store_id}/rag/faqs")
async def rag_add_faq(request: Request, store_id: str, body: FaqBody):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    ext = body.external_id or f"faq:{body.title[:40]}"
    # Keep legacy table in sync for dashboard
    training_svc.create_knowledge(
        store_id, title=body.title, content=body.content, entry_type="faq"
    )
    prep = upsert_and_index_async_prep(
        store_id,
        source_type="faq",
        external_id=ext,
        title=body.title,
        body=f"Q: {body.title}\nA: {body.content}",
    )
    if prep.get("skipped"):
        return {"success": True, **prep}
    result = await index_source_async(
        store_id, prep["source_id"], text=prep["text"], title=body.title
    )
    return {"success": True, "result": result}


@router.post("/{store_id}/rag/custom")
async def rag_add_custom(request: Request, store_id: str, body: CustomBody):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    ext = body.external_id or f"custom:{body.title[:40]}"
    training_svc.create_knowledge(
        store_id, title=body.title, content=body.content, entry_type="note"
    )
    prep = upsert_and_index_async_prep(
        store_id,
        source_type="custom",
        external_id=ext,
        title=body.title,
        body=f"{body.title}\n{body.content}",
    )
    if prep.get("skipped"):
        return {"success": True, **prep}
    result = await index_source_async(
        store_id, prep["source_id"], text=prep["text"], title=body.title
    )
    return {"success": True, "result": result}


@router.post("/{store_id}/rag/documents")
async def rag_upload_document(
    request: Request,
    store_id: str,
    file: UploadFile = File(...),
):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    raw = await file.read()
    name = file.filename or "document.txt"
    lower = name.lower()
    text = ""
    if lower.endswith((".txt", ".md", ".markdown", ".csv")):
        text = raw.decode("utf-8", errors="ignore")
    elif lower.endswith(".pdf"):
        # Minimal PDF text extraction without heavy deps
        try:
            import re

            text = " ".join(
                m.decode("utf-8", errors="ignore")
                for m in re.findall(rb"\(([^)]{4,})\)", raw)
            )
        except Exception:
            text = ""
        if len(text) < 40:
            raise HTTPException(
                status_code=400,
                detail="Could not extract PDF text. Convert to TXT/MD and re-upload.",
            )
    elif lower.endswith((".docx",)):
        raise HTTPException(
            status_code=400,
            detail="DOCX support: export as TXT/Markdown for now.",
        )
    else:
        text = raw.decode("utf-8", errors="ignore")

    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty document")

    prep = upsert_and_index_async_prep(
        store_id,
        source_type="document",
        external_id=f"doc:{name}",
        title=name,
        body=text[:200000],
        meta={"filename": name},
    )
    if prep.get("skipped"):
        return {"success": True, **prep}
    result = await index_source_async(
        store_id, prep["source_id"], text=prep["text"], title=name
    )
    return {"success": True, "result": result}


@router.get("/{store_id}/rag/exclusions")
def rag_list_exclusions(request: Request, store_id: str):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    return {"success": True, "exclusions": list_exclusions(store_id)}


@router.post("/{store_id}/rag/exclusions")
def rag_add_exclusion(request: Request, store_id: str, body: ExclusionBody):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    return {"success": True, "exclusion": add_exclusion(
        store_id, body.match_type, body.match_value, body.reason
    )}


@router.delete("/{store_id}/rag/exclusions/{exclusion_id}")
def rag_del_exclusion(request: Request, store_id: str, exclusion_id: str):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    if not delete_exclusion(store_id, exclusion_id):
        raise HTTPException(status_code=404, detail="Not found")
    return {"success": True}


@router.post("/{store_id}/rag/search/test")
async def rag_search_test(request: Request, store_id: str, body: SearchTestBody):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    hits = await search(store_id, body.query, top_k=body.top_k)
    return {"success": True, "query": body.query, "count": len(hits), "results": hits}


@router.get("/{store_id}/rag/jobs")
def rag_list_jobs(request: Request, store_id: str):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    return {"success": True, "jobs": list_jobs(store_id)}


@router.get("/{store_id}/rag/jobs/{job_id}")
def rag_get_job(request: Request, store_id: str, job_id: str):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    job = get_job(store_id, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "job": job}


@router.post("/{store_id}/rag/jobs")
async def rag_create_job(
    request: Request,
    store_id: str,
    body: SyncJobBody,
    background_tasks: BackgroundTasks,
):
    _auth_store(request, store_id)
    tenant = _tenant_or_404(store_id)
    job_type = body.job_type
    job = create_job(store_id, job_type)

    async def _run_items(items):
        await run_job(store_id, job["id"], items)

    if job_type in ("shopify_full_sync", "full_reindex") and (
        tenant.get("platform") or ""
    ).lower() == "shopify":
        items = await collect_shopify_knowledge(tenant)
        background_tasks.add_task(_run_items, items)
        return {"success": True, "job": job, "queued": len(items)}

    if job_type in ("woocommerce_full_sync", "woo_full_sync", "full_reindex"):
        platform = (tenant.get("platform") or "woocommerce").lower()
        if platform != "shopify" and woocommerce_sync_ready(tenant):
            items = await collect_woocommerce_knowledge(tenant)
            background_tasks.add_task(_run_items, items)
            return {"success": True, "job": job, "queued": len(items)}

    if job_type in ("crawl", "website_crawl"):
        settings = get_rag_settings(store_id)
        seeds = body.seed_urls or [
            u.strip()
            for u in (settings.get("crawler_seed_urls") or "").splitlines()
            if u.strip()
        ]
        items = await crawl_urls(seeds)
        background_tasks.add_task(_run_items, items)
        return {"success": True, "job": job, "queued": len(items)}

    if body.items:
        items = [i.model_dump() for i in body.items]
        background_tasks.add_task(_run_items, items)
        return {"success": True, "job": job, "queued": len(items)}

    if job_type == "rebuild_embeddings":
        background_tasks.add_task(rebuild_embeddings, store_id)
        return {"success": True, "job": job}

    raise HTTPException(
        status_code=400,
        detail="Provide items, or use shopify_full_sync / woocommerce_full_sync / crawl / rebuild_embeddings",
    )


@router.post("/{store_id}/rag/rebuild")
async def rag_rebuild(
    request: Request, store_id: str, background_tasks: BackgroundTasks
):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    background_tasks.add_task(rebuild_embeddings, store_id)
    return {"success": True, "message": "Rebuild started"}


@router.get("/{store_id}/rag/logs")
def rag_logs(request: Request, store_id: str, limit: int = 100):
    _auth_store(request, store_id)
    _tenant_or_404(store_id)
    return {"success": True, "logs": list_logs(store_id, limit=limit)}
