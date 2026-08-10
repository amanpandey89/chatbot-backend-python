(function () {

  // ── 1. Read configuration from the script tag ──────────────────────────
  // When WordPress outputs:
  // <script src="..." data-store-id="abc" data-backend-url="https://..."></script>
  // We read those data attributes here
  const scriptTag = document.currentScript;
  const STORE_ID = scriptTag.getAttribute('data-store-id');
  const BACKEND = scriptTag.getAttribute('data-backend-url');

  if (!STORE_ID || !BACKEND) {
    console.error('Chatbot: missing data-store-id or data-backend-url');
    return; // stop if config is missing
  }

  // ── 2. Internal state ──────────────────────────────────────────────────
  let sessionId = null;   // filled after /api/session call
  let isOpen = false;  // is the chat window open?
  let isLoading = false;  // are we waiting for a backend response?
  let currencySymbol = scriptTag.getAttribute('data-currency') || '₹';
  // filled/overridden after /api/session call

  // Shown only before the first user message in a new session
  const DEFAULT_QUICK_REPLIES = [
    "Track my order",
    "I want to return an item",
    "Recommend a phone",
    "Find accessories"
  ];

  function getQuickReplies() {
    if (Array.isArray(window.CB_QUICK_REPLIES) && window.CB_QUICK_REPLIES.length) {
      return window.CB_QUICK_REPLIES.map(String).filter(Boolean);
    }
    try {
      const attr = scriptTag.getAttribute('data-quick-replies');
      if (attr) {
        const parsed = JSON.parse(attr);
        if (Array.isArray(parsed) && parsed.length) {
          return parsed.map(String).filter(Boolean);
        }
      }
    } catch (e) { /* ignore */ }
    return DEFAULT_QUICK_REPLIES.slice();
  }

  // WordPress plugin sets window.CB_USER_CONTEXT and/or cookie cb_prefs
  function readCookie(name) {
    const match = document.cookie.match(new RegExp('(?:^|; )' + name.replace(/([.$?*|{}()[\]\\/+^])/g, '\\$1') + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function getUserContext() {
    let context = null;

    if (window.CB_USER_CONTEXT && typeof window.CB_USER_CONTEXT === 'object') {
      context = window.CB_USER_CONTEXT;
    } else {
      try {
        const attr = scriptTag.getAttribute('data-user-context');
        if (attr) context = JSON.parse(attr);
      } catch (e) { /* ignore bad JSON */ }
    }

    // Merge guest browsing cookie if plugin did not already include it
    try {
      const raw = readCookie('cb_prefs');
      if (raw) {
        const cookiePrefs = JSON.parse(raw);
        context = context || {
          auth_state: 'guest',
          visitor_id: null,
          customer_id: null,
          preferences: {},
          source: 'cookies'
        };
        context.preferences = Object.assign({}, cookiePrefs, context.preferences || {});
        if (!context.source || context.source === 'none') context.source = 'cookies';
      }
    } catch (e) { /* ignore bad cookie */ }

    return context;
  }

  // ── 3. Inject CSS styles ───────────────────────────────────────────────
  // We inject styles directly so the widget works on any theme
  // without needing a separate CSS file
  const style = document.createElement('style');
  style.textContent = `
    #cb-bubble {
      position: fixed;
      bottom: 24px;
      left: 24px;
      width: 56px;
      height: 56px;
      background: #6c47ff;
      border-radius: 50%;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 12px rgba(0,0,0,0.2);
      z-index: 99999;
      border: none;
      transition: transform 0.2s;
	  padding:5px;
    }
    #cb-bubble:hover { transform: scale(1.08); }
    #cb-bubble svg { width: 26px; height: 26px; fill: white; }

    #cb-window {
      position: fixed;
      bottom: 90px;
      left: 24px;
      width: 360px;
      height: 520px;
      background: #ffffff;
      border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.15);
      display: flex;
      flex-direction: column;
      z-index: 99998;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 14px;
    }
    #cb-window.cb-hidden { display: none; }

    #cb-header {
      background: #6c47ff;
      color: white;
      padding: 14px 16px;
      font-weight: 600;
      font-size: 15px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    #cb-close {
      background: none;
      border: none;
      color: white;
      font-size: 20px;
      cursor: pointer;
      line-height: 1;
      padding: 0;
    }

    #cb-messages {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      background: #f7f7f8;
    }

    .cb-msg {
      max-width: 85%;
      padding: 10px 14px;
      border-radius: 14px;
      line-height: 1.5;
      word-wrap: break-word;
      white-space: pre-wrap;
    }
    .cb-msg-bot {
      background: #ffffff;
      color: #1a1a1a;
      align-self: flex-start;
      border-bottom-left-radius: 4px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
      white-space: normal;
    }
    .cb-msg-bot p { margin: 0 0 8px; }
    .cb-msg-bot p:last-child { margin-bottom: 0; }
    .cb-msg-bot ul, .cb-msg-bot ol {
      margin: 6px 0 8px;
      padding-left: 1.2em;
    }
    .cb-msg-bot li { margin: 3px 0; }
    .cb-msg-bot strong { font-weight: 700; }
    .cb-msg-bot a { color: #6c47ff; text-decoration: underline; }
    .cb-msg-user {
      background: #6c47ff;
      color: white;
      align-self: flex-end;
      border-bottom-right-radius: 4px;
    }

    .cb-typing {
      background: #ffffff;
      color: #888;
      align-self: flex-start;
      padding: 10px 14px;
      border-radius: 14px;
      border-bottom-left-radius: 4px;
      font-style: italic;
      font-size: 13px;
    }

    .cb-product-card {
      background: #ffffff;
      border-radius: 12px;
      padding: 12px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.10);
      display: flex;
      flex-direction: column;
      gap: 6px;
      width: 100%;
      box-sizing: border-box;
    }
    .cb-product-card img {
      width: 100%;
      height: 140px;
      object-fit: cover;
      border-radius: 8px;
      background: #eee;
    }
    .cb-product-name {
      font-weight: 600;
      color: #1a1a1a;
      font-size: 14px;
    }
    .cb-product-price {
      color: #6c47ff;
      font-weight: 600;
      font-size: 14px;
    }
    .cb-product-reason {
      color: #555;
      font-size: 12px;
      line-height: 1.4;
    }
    .cb-product-btn {
      display: inline-block;
      margin-top: 4px;
      padding: 8px 14px;
      background: #6c47ff;
      color: white;
      border-radius: 8px;
      text-decoration: none;
      font-size: 13px;
      font-weight: 500;
      text-align: center;
      border: none;
      cursor: pointer;
      font-family: inherit;
    }
    .cb-product-btn:hover { background: #5538d6; color: white; }
    .cb-product-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 4px;
    }
    .cb-product-btn-secondary {
      background: #ffffff;
      color: #6c47ff;
      border: 1px solid #6c47ff;
    }
    .cb-product-btn-secondary:hover {
      background: #f3efff;
      color: #5538d6;
    }
    .cb-product-btn:disabled {
      background: #cccccc;
      border-color: #cccccc;
      color: #ffffff;
      cursor: default;
    }
    .cb-toast {
      align-self: center;
      background: #1a1a1a;
      color: #ffffff;
      font-size: 12px;
      padding: 6px 12px;
      border-radius: 999px;
      opacity: 0.92;
    }

    #cb-input-row {
      display: flex;
      padding: 12px;
      gap: 8px;
      background: #ffffff;
      border-top: 1px solid #eeeeee;
    }
    #cb-input {
      flex: 1;
      padding: 10px 14px;
      border: 1px solid #dddddd;
      border-radius: 24px;
      outline: none;
      font-size: 14px;
      font-family: inherit;
    }
    #cb-input:focus { border-color: #6c47ff; }
    #cb-send {
      width: 40px;
      height: 40px;
      background: #6c47ff;
      border: none;
      border-radius: 50%;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
	  padding:0;
    }
    #cb-send:hover { background: #5538d6; }
    #cb-send svg { width: 18px; height: 18px; fill: white; }
    #cb-send:disabled { background: #cccccc; cursor: default; }

    .cb-quick-replies {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
      width: 100%;
      box-sizing: border-box;
      margin-top: 4px;
    }
    .cb-quick-reply {
      background: #ffffff;
      color: #1a1a1a;
      border: 1px solid #dddddd;
      border-radius: 20px;
      padding: 8px 14px;
      font-size: 13px;
      font-family: inherit;
      line-height: 1.4;
      cursor: pointer;
      text-align: left;
      max-width: 100%;
      transition: background 0.15s, border-color 0.15s, color 0.15s;
    }
    .cb-quick-reply:hover {
      background: #f3efff;
      border-color: #6c47ff;
      color: #6c47ff;
    }
    .cb-quick-reply:active {
      background: #ebe4ff;
    }

    @media (max-width: 400px) {
      #cb-window { width: calc(100vw - 24px); right: 12px; }
      .cb-quick-reply { font-size: 12px; padding: 8px 12px; }
    }
  `;
  document.head.appendChild(style);

  // ── 4. Build the HTML structure ────────────────────────────────────────

  // The floating bubble button
  const bubble = document.createElement('button');
  bubble.id = 'cb-bubble';
  bubble.title = 'Chat with us';
  bubble.innerHTML = `
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M20 2H4C2.9 2 2 2.9 2 4v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/>
    </svg>`;
  document.body.appendChild(bubble);

  // The chat window
  const win = document.createElement('div');
  win.id = 'cb-window';
  win.className = 'cb-hidden';
  win.innerHTML = `
    <div id="cb-header">
      <span>Shopping Assistant</span>
      <button id="cb-close" title="Close">&#x2715;</button>
    </div>
    <div id="cb-messages"></div>
    <div id="cb-input-row">
      <input id="cb-input" type="text" placeholder="Type a message..." autocomplete="off"/>
      <button id="cb-send" disabled>
        <svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2v7z"/></svg>
      </button>
    </div>`;
  document.body.appendChild(win);

  // Shortcut references to inner elements
  const messagesEl = document.getElementById('cb-messages');
  const inputEl = document.getElementById('cb-input');
  const sendBtn = document.getElementById('cb-send');

  // ── 5. Helper functions ────────────────────────────────────────────────

  // Add a text message bubble to the chat window
  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function formatMessageText(text) {
    if (!text) return '';
    return String(text)
      // Put numbered steps on their own lines when AI returns them inline
      .replace(/\s+(\d+)\.\s+/g, '\n$1. ')
      // Keep paragraph breaks tidy
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  // Safe lightweight markdown → HTML for bot replies
  function formatMessageHtml(text) {
    let t = formatMessageText(text);
    t = escapeHtml(t);

    // Links [label](url) or bare https://
    t = t.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    t = t.replace(
      /(^|[\s(])(https?:\/\/[^\s<]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>'
    );

    // Bold **text** or __text__
    t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/__(.+?)__/g, '<strong>$1</strong>');

    // Italic *text* or _text_ (avoid matching inside words for _)
    t = t.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');

    // Convert bullet / numbered blocks to lists
    const lines = t.split('\n');
    const out = [];
    let listType = null; // 'ul' | 'ol'

    function closeList() {
      if (listType) {
        out.push(listType === 'ul' ? '</ul>' : '</ol>');
        listType = null;
      }
    }

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const bullet = line.match(/^\s*[-•]\s+(.+)$/);
      const numbered = line.match(/^\s*(\d+)\.\s+(.+)$/);

      if (bullet) {
        if (listType !== 'ul') {
          closeList();
          out.push('<ul>');
          listType = 'ul';
        }
        out.push('<li>' + bullet[1] + '</li>');
        continue;
      }
      if (numbered) {
        if (listType !== 'ol') {
          closeList();
          out.push('<ol>');
          listType = 'ol';
        }
        out.push('<li>' + numbered[2] + '</li>');
        continue;
      }

      closeList();
      if (line.trim() === '') {
        out.push('<br>');
      } else {
        out.push('<p>' + line + '</p>');
      }
    }
    closeList();
    return out.join('');
  }

  function addMessage(text, sender) {
    // sender is either 'bot' or 'user'
    const div = document.createElement('div');
    div.className = `cb-msg cb-msg-${sender}`;
    if (sender === 'bot') {
      div.innerHTML = formatMessageHtml(text);
    } else {
      div.textContent = text;
    }
    messagesEl.appendChild(div);
    scrollToBottom();
  }

  // Add a typing indicator (animated dots while waiting for response)
  function showTyping() {
    const div = document.createElement('div');
    div.className = 'cb-typing';
    div.id = 'cb-typing';
    div.textContent = 'Assistant is typing...';
    messagesEl.appendChild(div);
    scrollToBottom();
  }

  // Remove the typing indicator
  function hideTyping() {
    const el = document.getElementById('cb-typing');
    if (el) el.remove();
  }

  // Scroll chat to the bottom so latest message is always visible
  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // Render product recommendation cards
  function showToast(text) {
    const el = document.createElement('div');
    el.className = 'cb-toast';
    el.textContent = text;
    messagesEl.appendChild(el);
    scrollToBottom();
    setTimeout(function () { el.remove(); }, 2500);
  }

  async function addToCart(product, button) {
    const cart = window.CB_CART || {};
    const productId = typeof product === 'object' ? product.id : product;
    const variantId = typeof product === 'object' ? product.variant_id : null;
    if (!cart.enabled || !productId) {
      showToast('Add to cart is not available on this page.');
      return;
    }

    const original = button.textContent;
    button.disabled = true;
    button.textContent = 'Adding...';

    try {
      let res;
      let data = null;

      if ((cart.platform || '').toLowerCase() === 'shopify') {
        const id = variantId || productId;
        res = await fetch(cart.ajaxUrl || '/cart/add.js', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ id: id, quantity: 1 })
        });
        data = await res.json().catch(function () { return null; });
        if (!res.ok || (data && data.status && data.message)) {
          showToast((data && (data.description || data.message)) || 'Could not add to cart.');
          button.disabled = false;
          button.textContent = original;
          return;
        }
        document.documentElement.dispatchEvent(new CustomEvent('cart:refresh'));
      } else {
        if (!cart.ajaxUrl) {
          showToast('Add to cart is not available on this page.');
          button.disabled = false;
          button.textContent = original;
          return;
        }
        res = await fetch(cart.ajaxUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          credentials: 'same-origin',
          body: 'product_id=' + encodeURIComponent(productId) + '&quantity=1'
        });
        data = await res.json().catch(function () { return null; });

        if (res.ok && data && data.error) {
          showToast(data.error_message || 'Could not add to cart.');
          button.disabled = false;
          button.textContent = original;
          return;
        }
        document.body.dispatchEvent(new Event('wc_fragment_refresh'));
        if (typeof jQuery !== 'undefined') {
          jQuery(document.body).trigger('wc_fragment_refresh');
          jQuery(document.body).trigger('added_to_cart');
        }
      }

      button.textContent = 'Added';
      showToast('Added to cart');
      setTimeout(function () {
        button.disabled = false;
        button.textContent = original;
      }, 1500);
    } catch (err) {
      showToast('Could not add to cart. Please try again.');
      button.disabled = false;
      button.textContent = original;
    }
  }

  function addProductCards(message, products) {
    // First show the AI intro message
    addMessage(message, 'bot');

    // Then render one card per product
    products.forEach(function (p) {
      const card = document.createElement('div');
      card.className = 'cb-product-card';

      const imgSrc = p.image_url || 'https://via.placeholder.com/300x140?text=No+Image';
      const price = p.price ? currencySymbol + Number(p.price).toLocaleString('en-IN') : '';
      const regular = p.regular_price && p.regular_price !== p.price
        ? currencySymbol + Number(p.regular_price).toLocaleString('en-IN')
        : '';

      const cartEnabled = window.CB_CART && window.CB_CART.enabled;
      card.innerHTML =
        '<img src="' + imgSrc + '" alt="' + (p.name || '') + '" loading="lazy"/>' +
        '<div class="cb-product-name"></div>' +
        '<div class="cb-product-price">' +
          price +
          (regular ? '<span style="text-decoration:line-through;color:#999;font-size:12px;margin-left:6px;">' + regular + '</span>' : '') +
        '</div>' +
        '<div class="cb-product-reason"></div>' +
        '<div class="cb-product-actions">' +
          (cartEnabled
            ? '<button type="button" class="cb-product-btn cb-add-cart" data-product-id="' + p.id + '">Add to cart</button>'
            : '') +
          '<a href="' + (p.product_url || '#') + '" target="_blank" class="cb-product-btn' +
            (cartEnabled ? ' cb-product-btn-secondary' : '') + '">View Product</a>' +
        '</div>';

      card.querySelector('.cb-product-name').textContent = p.name || '';
      card.querySelector('.cb-product-reason').textContent = p.reason || '';

      const addBtn = card.querySelector('.cb-add-cart');
      if (addBtn) {
        addBtn.addEventListener('click', function () {
          addToCart(p, addBtn);
        });
      }

      messagesEl.appendChild(card);
    });

    scrollToBottom();
  }

  // Enable or disable the send button
  function setLoading(loading) {
    isLoading = loading;
    sendBtn.disabled = loading;
    inputEl.disabled = loading;
  }

  // Quick-reply suggestion buttons (UI only — uses sendMessage)
  function showQuickReplies() {
    hideQuickReplies();

    const container = document.createElement('div');
    container.id = 'cb-quick-replies';
    container.className = 'cb-quick-replies';

    getQuickReplies().forEach(function (label) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cb-quick-reply';
      btn.textContent = label;
      btn.dataset.reply = label;
      container.appendChild(btn);
    });

    container.addEventListener('click', function (e) {
      const btn = e.target.closest('.cb-quick-reply');
      if (!btn || isLoading || !sessionId) return;
      inputEl.value = btn.dataset.reply;
      sendMessage();
    });

    messagesEl.appendChild(container);
    scrollToBottom();
  }

  function hideQuickReplies() {
    const el = document.getElementById('cb-quick-replies');
    if (el) el.remove();
  }

  // ── 6. API call functions ──────────────────────────────────────────────

  // Call POST /api/session to start a conversation
  async function startSession() {
    try {
      const payload = { store_id: STORE_ID };
      const userContext = getUserContext();
      if (userContext) payload.user_context = userContext;

      if (!BACKEND || !STORE_ID) {
        addMessage('Chatbot is not configured (missing Backend URL or Store ID).', 'bot');
        return;
      }

      const res = await fetch(`${BACKEND}/api/session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }

      if (res.ok && data && data.success) {
        sessionId = data.session_id;
        if (data.currency_symbol) currencySymbol = data.currency_symbol;
        addMessage(data.greeting, 'bot');
        showQuickReplies();
        sendBtn.disabled = false; // enable send now that session exists
        return;
      }

      const detail = (data && (data.detail || data.message)) || '';
      if (res.status === 404) {
        addMessage('Store not found on the chat server. Check Store ID in plugin settings.', 'bot');
      } else if (res.status >= 500) {
        addMessage(
          detail
            ? ('Chat server error: ' + detail)
            : 'Chat server is temporarily unavailable. Please try again in a moment.',
          'bot'
        );
      } else {
        addMessage(
          detail || 'Sorry, could not start chat. Please try again later.',
          'bot'
        );
      }
    } catch (err) {
      console.error('ASA session error:', err);
      addMessage(
        'Cannot reach the chat server. Check Backend URL and that the server is online.',
        'bot'
      );
    }
  }

  // Call POST /api/chat with the user's message
  async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || isLoading || !sessionId) return;

    // Hide suggestions permanently once the user sends anything
    hideQuickReplies();

    // Show user message in chat
    addMessage(text, 'user');
    inputEl.value = '';

    // Show typing indicator while waiting
    setLoading(true);
    showTyping();

    try {
      const payload = {
        store_id: STORE_ID,
        session_id: sessionId,
        message: text
      };
      const userContext = getUserContext();
      if (userContext) payload.user_context = userContext;

      const res = await fetch(`${BACKEND}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      hideTyping();

      if (data.success) {
        const response = data.response;

        if (response.type === 'recommendations') {
          // Render product cards
          addProductCards(response.message, response.products);
        } else {
          // Render plain text question
          const text = response.message || response.content;
          addMessage(text, 'bot');
        }
      } else {
        addMessage('Something went wrong. Please try again.', 'bot');
      }

    } catch (err) {
      hideTyping();
      addMessage('Connection error. Please try again.', 'bot');
    }

    setLoading(false);
  }

  // ── 7. Event listeners ─────────────────────────────────────────────────

  // Open/close chat window when bubble is clicked
  bubble.addEventListener('click', function () {
    isOpen = !isOpen;
    if (isOpen) {
      win.classList.remove('cb-hidden');
      inputEl.focus();
      // Start session on first open only
      if (!sessionId) startSession();
    } else {
      win.classList.add('cb-hidden');
    }
  });

  // Close button inside the chat window
  document.getElementById('cb-close').addEventListener('click', function () {
    isOpen = false;
    win.classList.add('cb-hidden');
  });

  // Send on button click
  sendBtn.addEventListener('click', sendMessage);

  // Send on Enter key press
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') sendMessage();
  });

})();
// The whole file is wrapped in (function(){ ... })()
// This is called an IIFE — Immediately Invoked Function Expression
// It means all variables stay private inside and don't clash
// with any other JavaScript on your WordPress site