# src/main.py
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles  # ← add this import
from src.routes import register, session, products, chat

app = FastAPI(title="WooCommerce Chatbot API")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# Serve the static folder at /static URL
# So chatbot.js is available at: https://your-backend.com/static/chatbot.js
app.mount("/static", StaticFiles(directory="static"), name="static")  # ← add this line

app.include_router(register.router, prefix="/api")
app.include_router(session.router, prefix="/api")
app.include_router(products.router, prefix="/api")
app.include_router(chat.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
