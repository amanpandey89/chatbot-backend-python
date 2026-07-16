# WooCommerce AI Chatbot

AI-powered product recommendation chatbot for WooCommerce stores. Built with Python FastAPI + OpenAI GPT-4o-mini, deployed on Railway, embedded in WordPress via a JS widget.

---

## Tech Stack

- **Backend** — Python 3.12 + FastAPI + Uvicorn
- **AI** — OpenAI GPT-4o-mini
- **Products** — WooCommerce REST API
- **Hosting** — Railway
- **Frontend** — Vanilla JS widget + WordPress

---

## Project Structure

```
chatbot-backend-python/
├── src/
│   ├── main.py                  # Entry point
│   ├── routes/
│   │   ├── register.py          # POST /api/register
│   │   ├── session.py           # POST /api/session
│   │   ├── products.py          # GET  /api/products
│   │   └── chat.py              # POST /api/chat
│   └── services/
│       ├── store.py             # In-memory storage
│       ├── woocommerce.py       # WooCommerce integration
│       └── openai_service.py    # OpenAI integration
├── static/
│   └── chatbot.js               # WordPress widget
├── .env                         # Secret keys (never commit)
├── .env.example
├── Procfile
└── requirements.txt
```

---

## Local Setup

```bash
# 1. Clone and enter project
git clone https://github.com/yourusername/chatbot-backend-python.git
cd chatbot-backend-python

# 2. Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Mac/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure .env
cp .env.example .env
# Fill in your values (see Environment Variables below)

# 5. Run server
uvicorn src.main:app --reload --port 3000
```

Open `http://localhost:3000/docs` to test all endpoints interactively.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `STORE_ID` | Yes | Any unique string e.g. `my-store-001` |
| `WC_STORE_URL` | Yes | Store URL — no trailing slash |
| `WC_CONSUMER_KEY` | Yes | WooCommerce REST API key |
| `WC_CONSUMER_SECRET` | Yes | WooCommerce REST API secret |
| `WC_STORE_NAME` | No | Display name (default: My Store) |
| `INCLUDED_CATEGORIES` | No | Comma-separated category names to include |

---

## WooCommerce API Keys

Go to **WooCommerce → Settings → Advanced → REST API → Add Key**
- Permissions: `Read`
- Copy Consumer Key and Consumer Secret

---

## WordPress Integration

Add to `functions.php`:

```php
add_action( 'wp_footer', function() {

    $backend_url = 'https://your-railway-url.up.railway.app';
    $store_id    = 'your-store-id-here';
    $visitor_ip  = $_SERVER['REMOTE_ADDR'];

    $blocked_ips = [];  // IPs to block
    $allowed_ips = [];  // Whitelist — leave empty for everyone

    if ( current_user_can( 'manage_options' ) ) return;
    if ( ! empty( $allowed_ips ) && ! in_array( $visitor_ip, $allowed_ips, true ) ) return;
    if ( in_array( $visitor_ip, $blocked_ips, true ) ) return;

    printf(
        '<script src="%s/static/chatbot.js" data-store-id="%s" data-backend-url="%s" defer></script>',
        esc_url( $backend_url ),
        esc_attr( $store_id ),
        esc_url( $backend_url )
    );

} );
```

---

## Deploy to Railway

```bash
# 1. Push to GitHub
git add .
git commit -m "initial commit"
git push

# 2. Go to railway.app → New Project → Deploy from GitHub
# 3. Add all environment variables in Railway → Variables tab
# 4. Get your public URL from Settings → Domains
```

Verify deployment:
```
https://your-url.up.railway.app/health   → {"status": "ok"}
https://your-url.up.railway.app/debug    → shows registered stores
```
---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not connect" | Check `store_id` in `functions.php` matches `STORE_ID` in env. Visit `/debug` to confirm store is registered. |
| 403 from WooCommerce | Regenerate API keys. Add `SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1` to `.htaccess`. |
| Raw JSON in chat | Hard refresh with Ctrl+Shift+R after any backend update. |
| Railway deploy fails | Run `pip freeze > requirements.txt` with venv active. Confirm `Procfile` exists. |
| Products not showing | Visit `/api/products?store_id=xxx` to test. Check `INCLUDED_CATEGORIES` matches exact WooCommerce names. |

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/register` | Register a store |
| POST | `/api/session` | Start a chat session |
| GET | `/api/products` | Fetch store products |
| POST | `/api/chat` | Send message, get AI response |
| GET | `/health` | Health check |
| GET | `/debug` | View registered stores |
| GET | `/docs` | Interactive API docs |

---

## License

MIT
