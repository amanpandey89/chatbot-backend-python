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

- URL: `/app/{store_id}` (Shopify opens this automatically after install)
- **Train AI** — tone, standing instructions, FAQs / policies / rules
- **Chats** — view customer conversation history
- **Settings** — tenant API key for WordPress / integrations

Training is stored in SQLite and injected into the OpenAI system prompt on every chat.

### WordPress / API access

```http
X-Store-Id: your-store-id
X-Tenant-Key: <from merchant Settings>
```

Endpoints under `/api/tenant/{store_id}/...` for settings, knowledge CRUD, and chats.

---

## WordPress Integration

Use the plugin in `wordpress-plugin/ai-shopping-assistant/` (copy into `wp-content/plugins/` and activate).

1. Open **WP Admin → AI Assistant**
2. Set **Backend URL**, **Store ID**, and **Tenant API key** (from `/admin/stores/{id}` or `/app/{id}/settings`)
3. Open **AI Assistant → Train AI** to set tone, instructions, FAQs / policies
4. Optionally edit quick replies, visibility, and personalized picks

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
