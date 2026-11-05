// Empório da Música · Lia — chat front-end (no framework, no build step).
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const messagesEl = $("#messages");
  const form = $("#composer");
  const input = $("#input");
  const sendBtn = $("#sendBtn");
  const heroOrb = $("#heroOrb");
  const headOrb = $("#headOrb");
  const liaState = $("#liaState");
  const tplLia = $("#tplLia");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Autofocus only on desktop: on phones it would scroll the page and pop the keyboard.
  const focusInput = () => { if (window.matchMedia("(min-width: 961px)").matches) input.focus({ preventScroll: true }); };

  // Each page load starts a fresh conversation (the UI does not re-render old history).
  let sessionId = null;
  let busy = false;

  const GREETING =
    "Oi! Eu sou a *Lia*, assistente virtual da Empório da Música 🎸\n" +
    "Posso te ajudar com preços e disponibilidade de instrumentos, status do seu pedido, formas de pagamento, trocas e entregas. Como posso ajudar?";

  const ICONS = {
    catalog: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.2-4.2"/></svg>',
    promo: '<svg viewBox="0 0 24 24"><path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="7.5" r="1.2"/></svg>',
    order: '<svg viewBox="0 0 24 24"><path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="M3 7l9 4 9-4M12 11v10"/></svg>',
    policy: '<svg viewBox="0 0 24 24"><path d="M5 4h11l3 3v13H5z"/><path d="M9 10h6M9 14h6"/></svg>',
    human: '<svg viewBox="0 0 24 24"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/></svg>',
    guard: '<svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></svg>',
  };

  // ------------------------------------------------------------------ orb state
  function setOrbs(state, label) {
    heroOrb.dataset.state = state;
    headOrb.dataset.state = state;
    liaState.textContent = label;
  }
  const IDLE_LABEL = "online · assistente virtual";

  // Eyes follow the pointer (subtle), unless the orb is busy.
  window.addEventListener("pointermove", (e) => {
    if (reduceMotion) return;
    for (const orb of [heroOrb, headOrb]) {
      if (orb.dataset.state !== "idle") { orb.style.removeProperty("--lx"); orb.style.removeProperty("--ly"); continue; }
      const r = orb.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2);
      const dy = e.clientY - (r.top + r.height / 2);
      const d = Math.hypot(dx, dy) || 1;
      const max = r.width * 0.07;
      orb.style.setProperty("--lx", `${(dx / d) * Math.min(max, d / 12)}px`);
      orb.style.setProperty("--ly", `${(dy / d) * Math.min(max, d / 12)}px`);
    }
  });

  // ------------------------------------------------------------------ formatting
  const escapeHtml = (s) =>
    s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // WhatsApp-style text → safe HTML (escape first, then *bold*, bullet lists, paragraphs).
  function formatReply(text) {
    const inline = (s) => escapeHtml(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\*(?!\s)(.+?)\*/g, "<strong>$1</strong>");
    const blocks = [];
    let list = null;
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      const bullet = line.match(/^(?:[-•●*]|\d+[.)])\s+(.*)$/);
      if (bullet) {
        list = list || [];
        list.push(`<li>${inline(bullet[1])}</li>`);
        continue;
      }
      if (list) { blocks.push(`<ul>${list.join("")}</ul>`); list = null; }
      if (line) blocks.push(`<p>${inline(line)}</p>`);
    }
    if (list) blocks.push(`<ul>${list.join("")}</ul>`);
    return blocks.join("");
  }

  // Typewriter that keeps the final HTML structure: fill text nodes progressively.
  function typeInto(el, html, onDone) {
    el.innerHTML = html;
    if (reduceMotion) { onDone && onDone(); return; }
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push([walker.currentNode, walker.currentNode.nodeValue]);
    nodes.forEach(([n]) => (n.nodeValue = ""));
    const total = nodes.reduce((a, [, t]) => a + t.length, 0);
    const perTick = Math.max(1, Math.ceil(total / 160)); // ~2.5s max for long replies
    let i = 0, j = 0;
    el.classList.add("caret");
    const tick = () => {
      let budget = perTick;
      while (budget-- > 0 && i < nodes.length) {
        const [node, full] = nodes[i];
        node.nodeValue = full.slice(0, ++j);
        if (j >= full.length) { i++; j = 0; }
      }
      scrollDown();
      if (i < nodes.length) setTimeout(tick, 16);
      else { el.classList.remove("caret"); onDone && onDone(); }
    };
    tick();
  }

  // ------------------------------------------------------------------ rendering
  function scrollDown() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function addUser(text) {
    const art = document.createElement("article");
    art.className = "msg msg--user";
    art.innerHTML = `<div class="bubble">${escapeHtml(text).replace(/\n/g, "<br>")}</div>`;
    messagesEl.appendChild(art);
    scrollDown();
  }

  function addLia() {
    const node = tplLia.content.firstElementChild.cloneNode(true);
    messagesEl.appendChild(node);
    scrollDown();
    return node;
  }

  function chip(kind, label, title, failed) {
    const el = document.createElement("span");
    el.className = `chip chip--${kind}${failed ? " chip--fail" : ""}`;
    el.innerHTML = `${ICONS[kind] || ""}<span>${escapeHtml(label)}</span>`;
    el.title = title;
    return el;
  }

  function renderLia(node, data) {
    const chips = node.querySelector(".msg__chips");
    const bubble = node.querySelector(".bubble");
    const orb = node.querySelector(".orb");
    chips.innerHTML = "";
    const seen = new Set();
    (data.tools || []).forEach((t, k) => {
      const key = t.label + t.ok;
      if (seen.has(key)) return;
      seen.add(key);
      const c = chip(t.kind, t.label, `${t.name}(${JSON.stringify(t.input)})`, !t.ok);
      c.style.animationDelay = `${k * 70}ms`;
      chips.appendChild(c);
    });
    if ((data.events || []).some((e) => e.startsWith("prompt_injection") || e.startsWith("output_"))) {
      chips.appendChild(chip("guard", "proteção ativa", data.events.join(", ")));
    }
    if (data.error) bubble.classList.add("is-error");
    orb.dataset.state = "talking";
    setOrbs(data.error ? "error" : "talking", data.error ? "instabilidade no sistema" : "digitando…");
    typeInto(bubble, formatReply(data.reply), () => {
      orb.dataset.state = "idle";
      setOrbs("idle", IDLE_LABEL);
    });
  }

  // ------------------------------------------------------------------ actions
  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    document.querySelectorAll(".suggestions button").forEach((b) => (b.disabled = true));
    addUser(text);
    input.value = "";
    autoGrow();

    const node = addLia();
    const bubble = node.querySelector(".bubble");
    bubble.classList.add("thinking");
    bubble.innerHTML = "<i></i><i></i><i></i>";
    node.querySelector(".orb").dataset.state = "thinking";
    setOrbs("thinking", "consultando o sistema…");

    let data;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      data = await res.json();
      sessionId = data.session_id;
    } catch (err) {
      data = { reply: "Poxa, não consegui falar com o sistema agora. Tenta de novo em instantes? 🙏", tools: [], events: [], error: true };
      console.error(err);
    }
    bubble.classList.remove("thinking");
    renderLia(node, data);
    busy = false;
    sendBtn.disabled = false;
    document.querySelectorAll(".suggestions button").forEach((b) => (b.disabled = false));
    focusInput();
  }

  function greet() {
    messagesEl.innerHTML = "";
    const node = addLia();
    setOrbs("happy", IDLE_LABEL);
    setTimeout(() => renderLia(node, { reply: GREETING, tools: [], events: [] }), reduceMotion ? 0 : 450);
  }

  async function reset() {
    if (busy) return;
    const old = sessionId;
    sessionId = null;
    fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: old }) }).catch(() => {});
    greet();
  }

  function autoGrow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  }

  async function loadStatus() {
    const el = $("#storeStatus");
    try {
      const s = await (await fetch("/api/status")).json();
      const text = s.store.replace("ABERTA agora", "Loja aberta agora").replace("FECHADA agora", "Loja fechada agora");
      el.classList.add(s.open ? "is-open" : "is-closed");
      el.lastElementChild.textContent = text;
    } catch (_) {
      el.lastElementChild.textContent = "Seg–Sex 9h–18h · Sáb 9h–13h";
    }
  }

  // ------------------------------------------------------------------ wiring
  form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); }
  });
  $("#suggestions").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-q]");
    if (btn) send(btn.dataset.q);
  });
  $("#resetBtn").addEventListener("click", reset);

  loadStatus();
  greet();
  focusInput();
})();
