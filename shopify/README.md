# Shopify app (Theme App Extension)

This folder is a Shopify CLI app that embeds the shared `chatbot.js` widget into Online Store themes. The AI brain stays on the Python FastAPI backend.

## Prerequisites

1. [Shopify Partners](https://partners.shopify.com/) account + a development store
2. [Shopify CLI](https://shopify.dev/docs/api/shopify-cli) installed
3. Backend deployed with a public HTTPS URL (`APP_URL`)
4. Env vars on the backend:

```bash
SHOPIFY_API_KEY=...
SHOPIFY_API_SECRET=...
APP_URL=https://your-backend.example.com
SHOPIFY_SCOPES=read_products,read_orders,read_customers
```

## Partner Dashboard setup

1. Create an app → **Custom** or public
2. Copy API key / secret into backend `.env`
3. Allowed redirection URL(s):
   - `https://YOUR_BACKEND_URL/shopify/callback`
4. Update `shopify.app.toml` (`client_id`, `application_url`, `redirect_urls`)

## Install a store

Open:

```
https://YOUR_BACKEND_URL/shopify/install?shop=your-store.myshopify.com
```

Or use **Admin → Install Shopify**. After OAuth, the shop is registered as a tenant (`platform: shopify`, store id = `your-store.myshopify.com`).

## Deploy the theme extension

```bash
cd shopify
shopify app config link   # once
shopify app deploy
```

Then in the store:

**Online Store → Themes → Customize → App embeds → AI Shopping Chat → enable**

Set **Backend URL** to your FastAPI public URL.

## Local extension preview

```bash
cd shopify
shopify app dev
```

Use a tunnel / public `APP_URL` so OAuth and the widget script can load.
