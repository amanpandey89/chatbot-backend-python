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
  let currencySymbol = '₹'; // filled after /api/session call

  // Shown only before the first user message in a new session
  const QUICK_REPLIES = [
    "Track my order",
    "I want to return an item",
    "Recommend a phone",
    "Find accessories"
  ];

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
      max-width: 80%;
      padding: 10px 14px;
      border-radius: 14px;
      line-height: 1.5;
      word-wrap: break-word;
    }
    .cb-msg-bot {
      background: #ffffff;
      color: #1a1a1a;
      align-self: flex-start;
      border-bottom-left-radius: 4px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
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
    }
    .cb-product-btn:hover { background: #5538d6; color: white; }

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
  function addMessage(text, sender) {
    // sender is either 'bot' or 'user'
    const div = document.createElement('div');
    div.className = `cb-msg cb-msg-${sender}`;
    div.textContent = text;
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
  function addProductCards(message, products) {
    // First show the AI intro message
    addMessage(message, 'bot');

    // Then render one card per product
    products.forEach(function (p) {
      const card = document.createElement('div');
      card.className = 'cb-product-card';

      // Build image — use placeholder if no image
      const imgSrc = p.image_url || 'https://via.placeholder.com/300x140?text=No+Image';

      // Format price — add currency symbol
      const price = p.price ? currencySymbol + Number(p.price).toLocaleString('en-IN') : '';
      const regular = p.regular_price && p.regular_price !== p.price
        ? currencySymbol + Number(p.regular_price).toLocaleString('en-IN')
        : '';

      card.innerHTML = `
        <img src="${imgSrc}" alt="${p.name}" loading="lazy"/>
        <div class="cb-product-name">${p.name}</div>
        <div class="cb-product-price">
          ${price}
          ${regular ? '<span style="text-decoration:line-through;color:#999;font-size:12px;margin-left:6px;">' + regular + '</span>' : ''}
        </div>
        <div class="cb-product-reason">${p.reason}</div>
        <a href="${p.product_url}" target="_blank" class="cb-product-btn">View Product</a>
      `;
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

    QUICK_REPLIES.forEach(function (label) {
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
      const res = await fetch(`${BACKEND}/api/session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ store_id: STORE_ID })
      });
      const data = await res.json();

      if (data.success) {
        sessionId = data.session_id;
        if (data.currency_symbol) currencySymbol = data.currency_symbol;
        addMessage(data.greeting, 'bot');
        showQuickReplies();
        sendBtn.disabled = false; // enable send now that session exists
      } else {
        addMessage('Sorry, could not connect. Please try again later.', 'bot');
      }
    } catch (err) {
      addMessage('Connection error. Please check your internet.', 'bot');
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
      const res = await fetch(`${BACKEND}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          store_id: STORE_ID,
          session_id: sessionId,
          message: text
        })
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