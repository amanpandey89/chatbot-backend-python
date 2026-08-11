# AI Shopping Chatbot

AI-powered product recommendation chatbot for **WooCommerce** and **Shopify** stores. Built with Python FastAPI + OpenAI GPT-4o-mini, deployed on Railway, embedded via a shared JS widget (WordPress plugin or Shopify theme app embed).

---

## Tech Stack

- **Backend** — Python 3.12 + FastAPI + Uvicorn
- **AI** — OpenAI GPT-4o-mini
- **Catalogs** — WooCommerce REST API + Shopify Admin API
- **Hosting** — Railway
- **Storefronts** — Vanilla JS widget + WordPress plugin / Shopify theme extension

---

## Project Structure

```
chatbot-backend-python/
├── src/
│   ├── main.py
│   ├── routes/
│   │   ├── chat.py, session.py, products.py, register.py
│   │   ├── admin.py
│   │   └── shopify_app.py          # OAuth + uninstall webhook
│   └── services/
│       ├── store.py                # SQLite tenants + sessions
│       ├── catalog.py              # Woo / Shopify adapter
│       ├── woocommerce.py
│       ├── shopify_service.py
│       └── openai_service.py
├── static/chatbot.js
├── shopify/                        # Shopify CLI app + theme embed
├── wordpress-plugin/               # (local / gitignored)
├── .env.example
└── requirements.txt
```

---

## Local Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill OPENAI_API_KEY (+ Woo and/or Shopify vars)
uvicorn src.main:app --reload --port 3000
```

Open `http://localhost:3000/docs` for API docs, `/admin` for the dashboard.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `APP_URL` | For Shopify | Public HTTPS backend URL |
| `SHOPIFY_API_KEY` | For Shopify | Partners app API key |
| `SHOPIFY_API_SECRET` | For Shopify | Partners app API secret |
| `SHOPIFY_SCOPES` | No | Default `read_products,read_orders,read_customers` |
| `STORE_ID` / `WC_*` | Optional | Auto-register one Woo store on startup |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | No | Dashboard login (`admin` / `change-me`) |
| `APP_DB` | Recommended on Render | SQLite path, e.g. `/var/data/app.db` on a persistent disk |
| `TENANTS_MIGRATE` | No | Set `1` only to seed from `tenants.json` (default off) |

> **Render note:** Stores live in SQLite (`APP_DB`). Without a persistent Disk, every deploy wipes the DB. Do not commit `tenants.json` — it used to re-import old stores after each deploy.

---

## Shopify

1. Create an app in [Shopify Partners](https://partners.shopify.com/)
2. Set redirect URL: `https://YOUR_BACKEND/shopify/callback`
3. Put API key/secret + `APP_URL` in `.env`
4. Install: Admin → **Install Shopify**, or  
   `https://YOUR_BACKEND/shopify/install?shop=your-store.myshopify.com`
5. Deploy theme extension (see [`shopify/README.md`](shopify/README.md)):

```bash
cd shopify
shopify app config link
shopify app deploy
```

6. Enable **AI Shopping Chat** in Themes → Customize → App embeds and set Backend URL

Store ID for Shopify tenants is the shop domain (`your-store.myshopify.com`).

---

## Train AI & merchant dashboard

Each store gets its own merchant dashboard:

- URL: `/app/{store_id}` (login with merchant username/password; Shopify opens this automatically after install)
- **Train AI (RAG)** — brand voice, FAQs/custom notes, knowledge sources, sync jobs, search testing, logs
- **Chats** — view customer conversation history
- **Settings** — OpenAI key and integration keys (for authenticated merchants)

### RAG knowledge pipeline

Content is **not fine-tuned**. It is ingested, chunked (~650 tokens, 100 overlap), embedded with `text-embedding-3-small`, and retrieved per chat query (top‑k chunks injected into the prompt).

| Source | How it syncs |
|--------|----------------|
| Website crawl / FAQs / custom / documents | Merchant dashboard + REST APIs |
| Shopify products/collections/pages/blogs/policies | Merchant **Sync Shopify** + webhooks on create/update/delete |
| WooCommerce catalog | Sync from merchant dashboard (or crawl store URL) |

Key APIs under `/api/tenant/{store_id}/rag/...`: overview, sources, ingest, faqs, custom, documents, exclusions, jobs, search/test, rebuild, logs.

Training metadata + vectors are stored in SQLite (ready to migrate to Postgres/pgvector).

---

## WordPress Integration

Use the plugin in `wordpress-plugin/ai-shopping-assistant/` (copy into `wp-content/plugins/` and activate).

1. In **Admin → Stores**, register the WooCommerce store (Store ID + store URL matching the WP site)
2. In **WP Admin → AI Assistant → Settings**, set **Backend URL** and **Store ID**
3. Open **AI Assistant → Overview** → **Create merchant account** (username + password, first time only)
4. Click **Open Merchant Dashboard** and sign in with those credentials
5. Use the merchant dashboard for **Train AI**. The WP plugin only embeds the storefront widget (+ quick replies / visibility)

---

## Deploy to Railway

1. Push to GitHub and deploy from Railway
2. Set env vars (including `APP_URL` = your Railway domain)
3. Verify `/health` and `/admin`

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/register` | Register a store |
| POST | `/api/session` | Start a chat session |
| GET | `/api/products` | Fetch store products |
| POST | `/api/chat` | Send message, get AI response |
| GET | `/shopify/install` | Start Shopify OAuth |
| GET | `/shopify/callback` | OAuth callback |
| POST | `/shopify/webhooks/app-uninstalled` | Disable tenant on uninstall |
| GET | `/admin` | Admin dashboard |
| GET | `/health` | Health check |
| GET | `/docs` | Interactive API docs |

---

## License

MIT
