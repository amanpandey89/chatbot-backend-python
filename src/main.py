# src/main.py
from dotenv import load_dotenv

load_dotenv()

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from src.routes import register, session, products, chat
from src.services.store import register_tenant, get_tenant


# ── Lifespan — runs on startup and shutdown ──────────────────────────────
# This is the modern replacement for @app.on_event("startup")
@asynccontextmanager
async def lifespan(app: FastAPI):

    # ── STARTUP ────────────────────────────────────────────────────────
    store_id = os.getenv("STORE_ID")
    store_url = os.getenv("WC_STORE_URL")
    consumer_key = os.getenv("WC_CONSUMER_KEY")
    consumer_secret = os.getenv("WC_CONSUMER_SECRET")
    store_name = os.getenv("WC_STORE_NAME", "My Store")

    print("─" * 40)
    print(f"STORE_ID        : {store_id}")
    print(f"WC_STORE_URL    : {store_url}")
    print(f"CONSUMER_KEY    : {consumer_key[:6] if consumer_key else 'MISSING'}...")
    print(f"STORE_NAME      : {store_name}")
    print("─" * 40)

    if store_id and store_url and consumer_key and consumer_secret:
        register_tenant(
            store_id,
            {
                "store_url": store_url.rstrip("/"),
                "consumer_key": consumer_key,
                "consumer_secret": consumer_secret,
                "store_name": store_name,
            },
        )

        # Verify it was saved correctly
        saved = get_tenant(store_id)
        if saved:
            print(f"✓ Store auto-registered successfully")
            print(f"✓ store_id : {store_id}")
            print(f"✓ store_name: {store_name}")
        else:
            print("✗ ERROR: Store registration failed")
    else:
        print("✗ WARNING: Missing .env values — store NOT registered")
        print("  Make sure STORE_ID, WC_STORE_URL, WC_CONSUMER_KEY,")
        print("  WC_CONSUMER_SECRET are all set in your .env file")

    print("─" * 40)

    yield  # server runs here

    # ── SHUTDOWN ───────────────────────────────────────────────────────
    print("Server shutting down...")


# Pass lifespan to FastAPI
app = FastAPI(title="WooCommerce Chatbot API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(register.router, prefix="/api")
app.include_router(session.router, prefix="/api")
app.include_router(products.router, prefix="/api")
app.include_router(chat.router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Chatbot API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Debug route — confirms what is in memory right now ──────────────────
@app.get("/debug-env")
def debug_env():
    return {
        "STORE_ID": os.getenv("STORE_ID") or "MISSING",
        "WC_STORE_URL": os.getenv("WC_STORE_URL") or "MISSING",
        "WC_STORE_NAME": os.getenv("WC_STORE_NAME") or "MISSING",
        "WC_CONSUMER_KEY": (
            os.getenv("WC_CONSUMER_KEY", "")[:6] + "..."
            if os.getenv("WC_CONSUMER_KEY")
            else "MISSING"
        ),
        "WC_CONSUMER_SECRET": "set" if os.getenv("WC_CONSUMER_SECRET") else "MISSING",
        "OPENAI_API_KEY": "set" if os.getenv("OPENAI_API_KEY") else "MISSING",
    }
