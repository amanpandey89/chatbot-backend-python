"""Sync jobs: create, run, track progress, retry."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src.knowledge import _conn, _lock, log_event, new_id, now, overview_stats
from src.knowledge.ingest import list_sources, upsert_and_index_async_prep, index_source_async


def create_job(tenant_id: str, job_type: str, meta: Optional[dict] = None) -> Dict[str, Any]:
    job_id = new_id()
    ts = now()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO sync_jobs
                (id, tenant_id, job_type, status, progress_pct, totals_json, error,
                 created_at, started_at, finished_at)
                VALUES (?, ?, ?, 'queued', 0, ?, '', ?, NULL, NULL)
                """,
                (job_id, tenant_id, job_type, json.dumps(meta or {}), ts),
            )
            conn.commit()
    log_event(tenant_id, "job_created", f"Job {job_type} queued", meta={"job_id": job_id})
    return get_job(tenant_id, job_id) or {"id": job_id, "status": "queued"}


def get_job(tenant_id: str, job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT id, job_type, status, progress_pct, totals_json, error,
                       created_at, started_at, finished_at
                FROM sync_jobs WHERE id = ? AND tenant_id = ?
                """,
                (job_id, tenant_id),
            ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "job_type": row[1],
        "status": row[2],
        "progress_pct": row[3],
        "totals": json.loads(row[4] or "{}"),
        "error": row[5],
        "created_at": row[6],
        "started_at": row[7],
        "finished_at": row[8],
    }


def list_jobs(tenant_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    with _lock:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT id, job_type, status, progress_pct, totals_json, error,
                       created_at, started_at, finished_at
                FROM sync_jobs WHERE tenant_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
    return [
        {
            "id": r[0],
            "job_type": r[1],
            "status": r[2],
            "progress_pct": r[3],
            "totals": json.loads(r[4] or "{}"),
            "error": r[5],
            "created_at": r[6],
            "started_at": r[7],
            "finished_at": r[8],
        }
        for r in rows
    ]


def _update_job(job_id: str, **fields):
    sets = []
    args = []
    for k, v in fields.items():
        if k == "totals":
            sets.append("totals_json = ?")
            args.append(json.dumps(v))
        else:
            sets.append(f"{k} = ?")
            args.append(v)
    args.append(job_id)
    with _lock:
        with _conn() as conn:
            conn.execute(
                f"UPDATE sync_jobs SET {', '.join(sets)} WHERE id = ?", args
            )
            conn.commit()


def fail_job(job_id: str, error: str, totals: Optional[dict] = None):
    payload = {
        "status": "failed",
        "error": (error or "")[:500],
        "finished_at": now(),
        "progress_pct": 100,
    }
    if totals is not None:
        payload["totals"] = totals
    _update_job(job_id, **payload)


def mark_job_fetching(job_id: str):
    _update_job(
        job_id,
        status="running",
        started_at=now(),
        progress_pct=0,
        error="",
        totals={"phase": "fetching", "total": 0, "indexed": 0, "skipped": 0, "failed": 0},
    )


def has_active_job(tenant_id: str) -> bool:
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM sync_jobs
                WHERE tenant_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
    return bool(row)


async def run_job(tenant_id: str, job_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Process prepared items: each item is
    {source_type, external_id, title, url, body|html, meta?}
    """
    job = get_job(tenant_id, job_id)
    if not job:
        return {"error": "job_not_found"}

    _update_job(job_id, status="running", started_at=now(), progress_pct=0, error="")
    total = len(items)
    indexed = skipped = failed = 0
    first_error = ""

    for i, item in enumerate(items):
        try:
            prep = upsert_and_index_async_prep(
                tenant_id,
                source_type=item.get("source_type") or "custom",
                external_id=str(item.get("external_id") or new_id()),
                title=item.get("title") or "",
                url=item.get("url") or "",
                body=item.get("body") or "",
                html=item.get("html") or "",
                meta=item.get("meta"),
            )
            if prep.get("skipped"):
                skipped += 1
            else:
                result = await index_source_async(
                    tenant_id,
                    prep["source_id"],
                    text=prep["text"],
                    title=prep.get("title") or "",
                )
                if result.get("error"):
                    failed += 1
                    err = str(result.get("error"))
                    if not first_error:
                        first_error = err
                    log_event(
                        tenant_id,
                        "index_error",
                        err,
                        level="error",
                        meta={"item": item.get("external_id"), "job_id": job_id},
                    )
                else:
                    indexed += 1
        except Exception as e:
            failed += 1
            err = str(e)
            if not first_error:
                first_error = err
            log_event(
                tenant_id,
                "index_error",
                err,
                level="error",
                meta={"item": item.get("external_id"), "job_id": job_id},
            )

        pct = round(((i + 1) / max(total, 1)) * 100, 1)
        _update_job(
            job_id,
            progress_pct=pct,
            totals={"total": total, "indexed": indexed, "skipped": skipped, "failed": failed},
            error=(first_error[:500] if first_error else ""),
        )

    status = "completed"
    if failed and indexed:
        status = "completed_with_errors"
    elif failed and not indexed:
        status = "failed"

    _update_job(
        job_id,
        status=status,
        progress_pct=100,
        finished_at=now(),
        totals={"total": total, "indexed": indexed, "skipped": skipped, "failed": failed},
        error=(first_error[:500] if first_error else ""),
    )
    log_event(
        tenant_id,
        "job_completed",
        f"Job finished: indexed={indexed} skipped={skipped} failed={failed}",
        meta={"job_id": job_id, "first_error": first_error[:200] if first_error else ""},
    )
    return get_job(tenant_id, job_id) or {}


async def rebuild_embeddings(tenant_id: str) -> Dict[str, Any]:
    """Re-embed all sources that have document text."""
    job = create_job(tenant_id, "rebuild_embeddings")
    sources = list_sources(tenant_id, limit=5000)
    items = []
    with _lock:
        with _conn() as conn:
            for s in sources:
                doc = conn.execute(
                    "SELECT body_text, title FROM knowledge_documents WHERE source_id = ?",
                    (s["id"],),
                ).fetchone()
                if not doc or not (doc[0] or "").strip():
                    continue
                items.append(
                    {
                        "source_type": s["source_type"],
                        "external_id": s["external_id"],
                        "title": doc[1] or s["title"],
                        "url": s["url"],
                        "body": doc[0],
                        "meta": s.get("meta"),
                    }
                )
    # Force reindex by clearing hashes
    with _lock:
        with _conn() as conn:
            conn.execute(
                "UPDATE knowledge_sources SET content_hash='' WHERE tenant_id = ?",
                (tenant_id,),
            )
            conn.commit()
    return await run_job(tenant_id, job["id"], items)


def dashboard_bundle(tenant_id: str) -> Dict[str, Any]:
    return {
        "overview": overview_stats(tenant_id),
        "jobs": list_jobs(tenant_id, limit=10),
        "sources": list_sources(tenant_id, limit=50),
    }
