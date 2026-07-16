from typing import Dict, Any, List, TypedDict


class Session(TypedDict):
    store_id: str
    messages: List[Dict[str, str]]
    answers: Dict[str, str]


import json
import os

TENANTS_FILE = "tenants.json"
sessions: Dict[str, Session] = {}

tenants: dict = {}


def _load_tenants() -> Dict[str, Any]:
    if os.path.exists(TENANTS_FILE):
        with open(TENANTS_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_tenants(tenants_data: Dict[str, Any]):
    with open(TENANTS_FILE, "w") as f:
        json.dump(tenants_data, f, indent=4)


def register_tenant(store_id: str, data: dict):
    tenants = _load_tenants()
    tenants[store_id] = data
    _save_tenants(tenants)


def get_tenant(store_id: str):
    tenants = _load_tenants()
    return tenants.get(store_id)


def create_session(session_id: str, store_id: str):
    sessions[session_id] = Session(store_id=store_id, messages=[], answers={})


def get_session(session_id: str):
    return sessions.get(session_id)


def add_message(session_id: str, role: str, content: str):
    if session_id in sessions:
        sessions[session_id]["messages"].append({"role": role, "content": content})


def save_answer(session_id: str, key: str, value: str):
    if session_id in sessions:
        sessions[session_id]["answers"][key] = value


# Add these at the bottom of store.py


def get_all_tenants() -> dict:
    """Return all registered tenants — used for debug"""
    return tenants


def get_all_sessions() -> dict:
    """Return all active sessions — used for debug"""
    return sessions
