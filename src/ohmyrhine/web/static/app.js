"use strict";
// The page of `rhine web`. It talks to the server over one WebSocket (see web/server.py):
// it sends actions, and draws the state, the history and the events of replies as they come.
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const TOKEN = new URLSearchParams(location.search).get("t") || "";
  const PHONE = matchMedia("(max-width: 720px)");
  const NARROW = matchMedia("(max-width: 900px)");
  const TOUCH = matchMedia("(hover: none)");
  const NIGHT = matchMedia("(prefers-color-scheme: dark)");
  const BUSY = "角色还在说话：等一等，或者点停止";
  // Apple keyboards say ⌘ where others say Ctrl.
  const MOD = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";

  // --- icons: line drawings on a 24 px grid, in the current text colour ---------------

  const ICONS = {
    sidebar: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M9.5 4.5v15"/>',
    feather: '<path d="M19.5 4.5c-6-1-12 3-13 10l-2 5 5-2c7-1 11-7 10-13Z"/><path d="M6.5 17.5 15 9M9 15h5"/>',
    film: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="m10 9.5 5 2.5-5 2.5v-5Z"/>',
    gauge: '<path d="M4 19.5h16"/><path d="M7 16.5v-4M12 16.5v-9M17 16.5v-6"/>',
    code: '<path d="m8.5 7-5 5 5 5M15.5 7l5 5-5 5M13.5 4.5l-3 15"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>',
    layers: '<path d="m12 3.5 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
    palette: '<path d="M12 3.5a8.5 8.5 0 1 0 0 17c1.3 0 2-.8 2-1.8 0-1.7-1.2-1.7-1.2-3.2 0-1.1.9-2 2-2h2a3.7 3.7 0 0 0 3.7-3.7c0-3.5-3.8-6.3-8.5-6.3Z"/><circle cx="7.8" cy="11" r="1"/><circle cx="10" cy="7.3" r="1"/><circle cx="14.5" cy="7.3" r="1"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5V5M12 19v2.5M2.5 12H5M19 12h2.5M5.2 5.2 7 7M17 17l1.8 1.8M5.2 18.8 7 17M17 7l1.8-1.8"/>',
    moon: '<path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10Z"/>',
    contrast: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17M12 8h7.5M12 12h8.5M12 16h7.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    dot: '<circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>',
    down: '<path d="m6 9.5 6 6 6-6"/>',
    right: '<path d="m9.5 6 6 6-6 6"/>',
    arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
    arrowDown: '<path d="M12 5v14M6 13l6 6 6-6"/>',
    arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
    image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m4 17 5-4.5 4 3.5 3-2.5 4 3.5"/>',
    smile: '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 14.5c.9 1.2 2.1 1.8 3.5 1.8s2.6-.6 3.5-1.8"/><path d="M9.2 9.6v.01M14.8 9.6v.01" stroke-width="2.4"/>',
    quote: '<path d="M10 7H6.5A2.5 2.5 0 0 0 4 9.5V12a2 2 0 0 0 2 2h2v1.5A2.5 2.5 0 0 1 5.5 18M20 7h-3.5A2.5 2.5 0 0 0 14 9.5V12a2 2 0 0 0 2 2h2v1.5a2.5 2.5 0 0 1-2.5 2.5"/>',
    undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    hand: '<path d="M8 12.5V5.8a1.4 1.4 0 0 1 2.8 0V11"/><path d="M10.8 10.5V4.4a1.4 1.4 0 0 1 2.8 0v6.1"/><path d="M13.6 10.6V5.6a1.4 1.4 0 0 1 2.8 0v6.4"/><path d="M16.4 11.2V8.4a1.4 1.4 0 0 1 2.8 0v5.3c0 4-2.9 7.3-6.9 7.3h-.6c-2 0-3.7-.9-4.9-2.4L4.4 14.9a1.5 1.5 0 0 1 2.3-1.9L8 14.3"/>',
    refresh: '<path d="M20 11A8 8 0 0 0 5.1 8M4 13a8 8 0 0 0 14.9 3"/><path d="M5 4v4h4M19 20v-4h-4"/>',
    history: '<path d="M4 12a8 8 0 1 0 2.5-5.8L4 8.5"/><path d="M4 4v4.5h4.5M12 8v4.5l3 1.8"/>',
    copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
    trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5M10 11v5M14 11v5"/>',
    more: '<circle cx="5.5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="18.5" cy="12" r="1.3"/>',
    warn: '<path d="M12 4 2.5 20h19L12 4Z"/><path d="M12 10v4.5M12 17.5v.01"/>',
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.5v.01"/>',
    user: '<circle cx="12" cy="8" r="3.8"/><path d="M4.5 20.5c0-4 3.4-7 7.5-7s7.5 3 7.5 7"/>',
    users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17.2" cy="9.4" r="2.4"/><path d="M17 14.2c2.4.3 4.2 2.4 4.2 5"/>',
    userPlus: '<circle cx="10" cy="8" r="3.5"/><path d="M3.5 20c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5"/><path d="M18.5 7.5v5M16 10h5"/>',
    userMinus: '<circle cx="10" cy="8" r="3.5"/><path d="M3.5 20c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5"/><path d="M16 10h5"/>',
    chat: '<path d="M20 11.5c0 4.1-3.6 7.5-8 7.5-1 0-2-.2-2.9-.5L4.5 20l1.2-3.6A7.2 7.2 0 0 1 4 11.5C4 7.4 7.6 4 12 4s8 3.4 8 7.5Z"/>',
    wand: '<path d="m4 20 11-11M13 7l4 4"/><path d="M18 3v3M16.5 4.5h3M20.5 10v2M19.5 11h2M10 3v2M9 4h2"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
    book: '<path d="M4.5 5.5A2.5 2.5 0 0 1 7 3h12.5v15H7a2.5 2.5 0 0 0-2.5 2.5v-15Z"/><path d="M4.5 20.5A2.5 2.5 0 0 0 7 21h12.5"/>',
    download: '<path d="M12 5v11M7 11l5 5 5-5M5 19.5h14"/>',
    keyboard: '<rect x="3" y="6.5" width="18" height="11" rx="2"/><path d="M7 10h.01M10.3 10h.01M13.7 10h.01M17 10h.01M7.5 14h9"/>',
    sparkles: '<path d="m11 4 1.6 4.4L17 10l-4.4 1.6L11 16l-1.6-4.4L5 10l4.4-1.6L11 4Z"/><path d="m18 14.5.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2Z"/>',
    ghost: '<path d="M5 20v-9.5a7 7 0 0 1 14 0V20l-2.3-2-2.4 2-2.3-2-2.3 2-2.4-2L5 20Z"/><path d="M9.5 10.5v.01M14.5 10.5v.01"/>',
  };

  function icon(name, cls = "") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", cls ? `icon ${cls}` : "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = ICONS[name] || ICONS.dot; // fixed drawings above, never text from anyone
    return svg;
  }
  for (const el of $$("[data-icon]")) el.replaceChildren(icon(el.dataset.icon));
  for (const el of $$("[data-icon-before]")) el.prepend(icon(el.dataset.iconBefore));

  // Builds an element; text always goes in as text, never as markup.
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const kbd = (text) => h("kbd", { class: "kbd" }, text);

  const saved = {
    get(key, fallback) {
      try { const v = localStorage.getItem("ohmyrhine." + key); return v == null ? fallback : JSON.parse(v); }
      catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem("ohmyrhine." + key, JSON.stringify(value)); } catch { /* private mode */ } },
  };

  const S = {
    ws: null, retry: 0, state: null, chat: null, messages: [], live: null, busy: false,
    quote: null, staged: [], panel: null, slash: [], slashAt: 0, models: null, stats: null,
    // What was seen while a message came in (thinking, tool use, the judge's verdict), by keyOf.
    notes: {},
    // Messages already drawn: only new ones rise in.
    seen: new Set(), shownChat: null, animate: true, stuck: true,
    showThinking: saved.get("thinking", true),
    theme: saved.get("theme", "system"),
    dark: saved.get("dark", "graphite"),
  };

  // --- theme -------------------------------------------------------------------------

  const THEMES = [
    { id: "system", label: "跟随系统", icon: "contrast" },
    { id: "light", label: "浅色", icon: "sun" },
    { id: "graphite", label: "石墨", icon: "moon" },
    { id: "ink", label: "墨黑", icon: "moon" },
  ];

  function applyTheme() {
    const theme = S.theme === "system" ? (NIGHT.matches ? S.dark : "light") : S.theme;
    document.documentElement.dataset.theme = theme;
    $("#theme-btn").replaceChildren(icon(theme === "light" ? "sun" : "moon"));
    // The browser's own bars take the page's colour.
    $('meta[name="theme-color"]').content = getComputedStyle(document.body).backgroundColor;
  }

  function setTheme(id, quiet = false) {
    S.theme = id; saved.set("theme", id); applyTheme();
    if (!quiet && S.panel === "settings") renderSettings();
  }

  function setDark(id) { S.dark = id; saved.set("dark", id); applyTheme(); }

  NIGHT.addEventListener("change", applyTheme);

  // --- the travelling mark under a row of choices (data-slide) ---------------------------
  // The mark is the row's ::after, from --slide-a to --slide-b; when the choice changes it
  // travels with a spring, the edge in front leaving a moment before the one behind.

  const CHOSEN = '[aria-pressed="true"], [aria-current="true"]';
  const slideSized = new WeakSet();
  const slideResize = new ResizeObserver(() => slideSchedule());
  let slideFrame = 0;

  function slidePlace(row) {
    if (!slideSized.has(row)) { slideSized.add(row); slideResize.observe(row); }
    const item = $$(CHOSEN, row).find((x) => x.closest("[data-slide]") === row);
    if (!item || !item.offsetParent) { row.removeAttribute("data-slide-on"); return; }
    const rowBox = row.getBoundingClientRect(), box = item.getBoundingClientRect(), style = getComputedStyle(item);
    const left = box.left - rowBox.left - row.clientLeft + row.scrollLeft + parseFloat(style.paddingLeft);
    const right = box.right - rowBox.left - row.clientLeft + row.scrollLeft - parseFloat(style.paddingRight);
    const was = parseFloat(row.style.getPropertyValue("--slide-a"));
    const first = !row.hasAttribute("data-slide-on");
    if (!first && Number.isFinite(was) && Math.abs(was - left) < 0.5) { row.style.setProperty("--slide-b", `${right}px`); return; }
    row.dataset.slideDir = first || !Number.isFinite(was) ? "none" : left > was ? "next" : "prev";
    if (first) row.removeAttribute("data-slide-ready");
    row.style.setProperty("--slide-a", `${left}px`);
    row.style.setProperty("--slide-b", `${right}px`);
    row.setAttribute("data-slide-on", "");
    if (first) { void row.offsetWidth; requestAnimationFrame(() => row.setAttribute("data-slide-ready", "")); }
  }

  function slideSchedule() {
    if (!slideFrame) slideFrame = requestAnimationFrame(() => { slideFrame = 0; for (const row of $$("[data-slide]")) slidePlace(row); });
  }

  const slideWatch = new MutationObserver(slideSchedule);
  for (const el of [$("#nav"), $("#drawer")]) {
    slideWatch.observe(el, { subtree: true, childList: true, attributes: true, attributeFilter: ["aria-pressed", "aria-current", "hidden"] });
  }
  addEventListener("resize", slideSchedule);
  document.fonts?.ready.then(slideSchedule);

  // --- menus, toasts, dialogs, pictures -------------------------------------------------

  let menu = null;

  function closeMenu() {
    if (!menu) return;
    menu.el.remove();
    menu.anchor.removeAttribute("aria-expanded");
    menu.cleanup();
    menu = null;
  }

  // items: {label, icon | face: {name, hue}, sub, hint, danger, disabled, onSelect} | "sep" | {title}
  function openMenu(anchor, items, { align = "start" } = {}) {
    if (menu?.anchor === anchor) { closeMenu(); return; }
    closeMenu();
    const el = h("div", { class: "menu", role: "menu" });
    for (const item of items) {
      if (!item) continue;
      if (item === "sep") { el.append(h("div", { class: "menu-sep" })); continue; }
      if (item.title) { el.append(h("div", { class: "menu-label" }, item.title)); continue; }
      el.append(h("button", {
        class: item.danger ? "menu-item danger" : "menu-item", type: "button", role: "menuitem", disabled: item.disabled,
        onclick: () => { closeMenu(); item.onSelect?.(); },
      }, item.face ? face(item.face.name, item.face.hue, 24) : item.icon ? icon(item.icon) : null,
      h("span", { class: "grow" }, h("span", { class: "ellipsis", style: "display: block" }, item.label),
        item.sub ? h("span", { class: "sub ellipsis" }, item.sub) : null),
      item.hint ? h("span", { class: "hint" }, item.hint) : null));
    }
    el.style.cssText = "left: -9999px; top: 0";
    document.body.append(el);
    const rect = anchor.getBoundingClientRect(), box = el.getBoundingClientRect();
    let left = align === "end" ? rect.right - box.width : rect.left;
    let top = rect.bottom + 6, originY = "top";
    if (top + box.height > innerHeight - 8) { top = Math.max(8, rect.top - box.height - 6); originY = "bottom"; }
    left = Math.min(Math.max(8, left), innerWidth - box.width - 8);
    el.style.cssText = `left: ${left}px; top: ${top}px; --menu-origin: ${originY} ${align === "end" ? "right" : "left"}`;
    anchor.setAttribute("aria-expanded", "true"); // and its hint gives way to the menu
    const away = (e) => { if (!el.contains(e.target) && !anchor.contains(e.target)) closeMenu(); };
    const keys = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(); anchor.focus?.(); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const all = $$(".menu-item:not(:disabled)", el);
        const at = all.indexOf(document.activeElement);
        all[(at + (e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length]?.focus();
      }
    };
    const timer = setTimeout(() => {
      addEventListener("pointerdown", away, true);
      addEventListener("keydown", keys, true);
      addEventListener("resize", closeMenu);
      $("#thread").addEventListener("scroll", closeMenu, { passive: true });
    }, 0);
    menu = {
      el, anchor, cleanup: () => {
        clearTimeout(timer);
        removeEventListener("pointerdown", away, true);
        removeEventListener("keydown", keys, true);
        removeEventListener("resize", closeMenu);
        $("#thread").removeEventListener("scroll", closeMenu);
      },
    };
  }

  function toast(text, kind = "info", { sticky = false } = {}) {
    const mark = kind === "wait" ? h("i", { class: "spin" }) : icon(kind === "error" ? "warn" : kind === "success" ? "check" : "info");
    const el = h("div", { class: "toast", "data-kind": kind }, mark, h("span", {}, text));
    const box = $("#toasts");
    box.append(el);
    while (box.children.length > 4) box.firstElementChild.remove();
    const dismiss = () => {
      if (!el.isConnected || el.classList.contains("leaving")) return;
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 200);
    };
    if (!sticky) setTimeout(dismiss, kind === "error" ? 8000 : 3500);
    return dismiss;
  }

  // A question in a small window; resolves to true / the text typed, or null when cancelled.
  function dialog({ title, body = null, input = null, ok = "确定", danger = false }) {
    return new Promise((resolve) => {
      const field = input ? h("input", { class: "input", type: "text", placeholder: input.placeholder || "", value: input.value || "" }) : null;
      const done = (value) => { scrim.remove(); removeEventListener("keydown", keys, true); resolve(value); };
      const yes = () => done(field ? field.value : true);
      const keys = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(null); }
        else if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); e.stopPropagation(); yes(); }
      };
      const scrim = h("div", { class: "scrim centered", onmousedown: (e) => { if (e.target === scrim) done(null); } },
        h("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title },
          h("h2", {}, title), body ? h("p", {}, body) : null, field,
          h("div", { class: "dialog-actions" },
            h("button", { class: "btn", type: "button", onclick: () => done(null) }, "取消"),
            h("button", { class: danger ? "btn primary danger" : "btn primary", type: "button", onclick: yes }, ok))));
      closeMenu();
      document.body.append(scrim);
      addEventListener("keydown", keys, true);
      (field || $(".btn.primary", scrim)).focus();
    });
  }

  function lightbox(src, caption) {
    const close = () => { scrim.remove(); removeEventListener("keydown", keys, true); };
    const keys = (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); } };
    const scrim = h("div", { class: "scrim centered lightbox", onclick: close },
      h("figure", {}, h("img", { src, alt: caption || "" }), caption ? h("figcaption", {}, caption) : null));
    document.body.append(scrim);
    addEventListener("keydown", keys, true);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Not a secure context (another device over plain http): the old way.
      const area = h("textarea", { style: "position: fixed; opacity: 0" }, text);
      document.body.append(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      if (!ok) { toast("没能复制：浏览器不允许", "error"); return; }
    }
    toast("已复制", "success");
  }

  // --- connection ------------------------------------------------------------------

  let lost = null;

  function connect() {
    if (!TOKEN) { toast("地址里缺少 token：用 rhine web 打印出来的链接打开", "error", { sticky: true }); return; }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
    S.ws = ws;
    ws.onopen = () => {
      S.retry = 0;
      lost?.(); lost = null;
      send({ type: "open", character: saved.get("character", null), session: saved.get("session", null),
             user: saved.get("user", null), nickname: saved.get("nickname", null) });
    };
    ws.onmessage = (e) => handle(JSON.parse(e.data));
    ws.onclose = () => {
      if (S.ws !== ws) return;
      S.busy = false; S.live = null; renderBusy(); renderLive();
      lost ||= toast("连接断开了，正在重连…", "wait", { sticky: true });
      setTimeout(connect, Math.min(8000, 500 * 2 ** S.retry++));
    };
  }

  function send(frame) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) { S.ws.send(JSON.stringify(frame)); return true; }
    toast("还没连上服务器", "error");
    return false;
  }

  function handle(f) {
    switch (f.type) {
      case "state": {
        const was = S.chat?.id;
        S.state = f.state;
        S.chat = f.state.chat;
        if (S.chat) {
          saved.set("character", S.chat.character.ref);
          saved.set("session", S.chat.id);
          S.busy = S.chat.busy;
        }
        renderSide(); renderHead(); renderBusy();
        if (!S.chat) renderMessages();
        if (S.panel === "settings") {
          // Not while something is being typed there.
          if (!$("#drawer").contains(document.activeElement) || !document.activeElement.matches("input, textarea, select")) renderSettings();
        } else if (S.panel && was !== S.chat?.id) openPanel(S.panel);
        break;
      }
      case "history": {
        if (S.chat && f.chat !== S.chat.id) break;
        const fresh = f.chat !== S.shownChat;
        if (fresh) { S.seen = new Set(); S.shownChat = f.chat; }
        else {
          // What was shown at once when sent is the same message: it doesn't rise in again.
          for (const m of f.messages) {
            if (m.kind === "me" && S.messages.some((e) => e.pending && e.text === m.text)) S.seen.add(keyOf(m));
          }
        }
        S.messages = f.messages;
        renderMessages(fresh, !fresh);
        break;
      }
      case "busy":
        S.busy = f.busy;
        if (!f.busy) S.live = null;
        renderBusy(); renderLive();
        break;
      case "speaking":
        S.live = { ref: f.ref, name: f.name, text: "", thinking: "", tools: [], status: "typing", thinkOpen: null };
        renderLive();
        break;
      case "looking":
        live(f).status = "looking"; renderLive();
        break;
      case "thinking":
        live(f).thinking += f.text; renderLive();
        break;
      case "delta":
        live(f).text += f.text; S.live.status = "text"; renderLive();
        break;
      case "tool":
        live(f).tools.push(f.text); renderLive();
        break;
      case "done":
        live(f).stop = f.stop;
        break;
      case "said":
        said(f);
        break;
      case "judged": {
        const m = lastMessage((x) => x.kind !== "character");
        if (m) {
          const verdict = (f.reply ? "接话" : "不接话") + (f.wait ? "，话还没说完" : "");
          note(keyOf(m)).after.push({ icon: "eye", text: `${f.name}的判断：${verdict} · ${f.reason}` });
          renderMessages();
        }
        break;
      }
      case "notice":
        toast(f.text, f.level === "info" ? "info" : "error");
        break;
      case "inspect":
        renderPanel(f.what, f.data);
        break;
      case "prefill":
        input.value = f.text; autosize(); input.focus();
        break;
    }
  }

  function live(f) {
    if (!S.live) S.live = { ref: f.ref, name: f.name, text: "", thinking: "", tools: [], status: "typing", thinkOpen: null };
    return S.live;
  }

  // Every character's copy of a message has the same platform id; system lines have only their number.
  const keyOf = (m) => m.pid || `#${m.id}`;

  function note(key) {
    return (S.notes[key] ||= { thinking: "", tools: [], after: [] });
  }

  function lastMessage(pred = () => true) {
    for (let i = S.messages.length - 1; i >= 0; i--) if (S.messages[i].id > 0 && pred(S.messages[i])) return S.messages[i];
    return null;
  }

  function said(f) {
    const was = S.live;
    S.live = null;
    if (f.message) {
      const key = keyOf(f.message);
      const n = note(key);
      if (was) {
        n.thinking = was.thinking; n.tools = was.tools.slice(); n.stop = was.stop;
        S.seen.add(key); // it was on the screen already, as it was being written
      }
      S.messages.push(f.message);
    } else if (was) {
      const m = lastMessage();
      if (m) {
        const n = note(keyOf(m));
        n.after.push(...was.tools.map((t) => ({ icon: toolIcon(t), text: t })));
        n.after.push({ icon: "chat", text: was.stop === "silent" ? `${was.name}没有回复` : `${was.name}没有新的消息可回` });
      }
    }
    renderMessages();
  }

  // --- faces -------------------------------------------------------------------------

  // The first character of a chat takes the accent's hue, the others the next ones.
  const HUES = [275, 200, 25, 150, 80, 330];

  function hueOf(m) {
    if (m.kind === "character" && S.chat) {
      const i = S.chat.cast.findIndex((c) => c.ref === m.ref);
      if (i >= 0) return HUES[i % HUES.length];
    }
    let n = 7;
    for (const ch of m.sender || m.who || "") n = (n * 31 + ch.codePointAt(0)) >>> 0;
    return HUES[1 + (n % (HUES.length - 1))];
  }

  function face(name, hue, size = 36, cls = "") {
    return h("span", { class: cls ? `face ${cls}` : "face", style: `--hue: ${hue}; --size: ${size}px`, "aria-hidden": "true" },
      [...(name || "?")][0]);
  }

  // --- sessions and the header ---------------------------------------------------------

  function renderSide() {
    const c = S.chat;
    $("#who-switch").replaceChildren(...(c
      ? [face(c.character.name, HUES[0], 34), h("span", { class: "grow" }, h("strong", { class: "ellipsis" }, c.character.name),
          h("small", { class: "ellipsis" }, c.character.ref)), icon("down")]
      : [face("?", HUES[0], 34, "plain"), h("span", { class: "grow" }, h("strong", {}, "还没有角色"), h("small", {}, "rhine new")), icon("down")]));
    const sessions = c?.sessions || [];
    const items = sessions.map((s) => h("li", {}, h("button", {
      class: "session", type: "button", title: s.id, "aria-current": s.id === c.id ? "true" : null,
      onclick: () => { if (s.id !== c.id) send({ type: "resume", id: s.id }); closeSide(); },
    }, h("span", { class: "top" }, h("span", { class: "title" }, s.label), h("span", { class: "time" }, s.time)),
    h("span", { class: "preview" }, s.preview || "（还没有消息）"))));
    if (c && !sessions.some((s) => s.id === c.id)) {
      items.unshift(h("li", {}, h("button", { class: "session", type: "button", "aria-current": "true" },
        h("span", { class: "top" }, h("span", { class: "title" }, "新会话")), h("span", { class: "preview" }, "（还没有消息）"))));
    }
    $("#sessions").replaceChildren(...items);
    $("#session-count").textContent = sessions.length ? String(sessions.length) : "";
    const me = c?.me;
    $("#me").replaceChildren(...(me ? [face(me.nickname, 0, 30, "plain"), h("span", { class: "grow" },
      h("strong", { class: "ellipsis" }, me.nickname), h("small", { class: "ellipsis" }, me.id)), icon("settings")] : []));
  }

  function renderHead() {
    const c = S.chat;
    document.title = c ? `${c.character.name} · ohMyRhine` : "ohMyRhine";
    const head = $("#chat-head");
    if (!c) { head.replaceChildren(h("div", { class: "grow" }, h("h1", {}, "ohMyRhine"))); return; }
    head.replaceChildren(
      h("span", { class: "faces" }, c.cast.slice(0, 4).map((m, i) => face(m.name, HUES[i % HUES.length], 30))),
      h("div", { class: "grow" }, h("h1", { title: c.id }, c.label),
        h("div", { class: "sub" }, h("span", {}, c.kind === "group" ? "群聊" : "私聊"), h("span", { class: "mono" }, c.model),
          c.usage.cost ? h("span", { class: "num", title: "这次运行的估算费用" }, `$${c.usage.cost.toFixed(4)}`) : null)),
      h("button", { class: "btn icon", type: "button", "aria-label": "拉角色进来", "data-tip": "拉角色进来",
        onclick: (e) => inviteMenu(e.currentTarget) }, icon("userPlus")),
      h("button", { class: "btn icon", type: "button", "aria-label": "更多", "data-tip": "更多", "data-tip-side": "end",
        onclick: (e) => moreMenu(e.currentTarget) }, icon("more")));
  }

  function characterMenu(anchor) {
    const packs = S.state?.characters || [];
    const current = S.chat?.character.ref;
    openMenu(anchor, [
      { title: "和谁聊" },
      ...packs.map((p) => ({ face: { name: p.name, hue: HUES[0] }, label: p.name, sub: p.identity, hint: p.id === current ? "当前" : p.id,
        onSelect: () => { if (p.id !== current) openCharacter(p.id); } })),
      packs.length ? null : { label: "还没有角色：先在命令行里用 rhine new 创建", disabled: true },
    ]);
  }

  function openCharacter(id) {
    S.notes = {};
    send({ type: "open", character: id, session: null });
    closeSide();
  }

  function inviteMenu(anchor) {
    const c = S.chat;
    const inCast = new Set(c.cast.map((m) => m.ref.split(":")[0]));
    const packs = (S.state.characters || []).filter((p) => !inCast.has(p.id));
    const guests = c.cast.filter((m) => !m.first);
    const hue = HUES[c.cast.length % HUES.length];
    openMenu(anchor, [
      { title: "拉进对话" },
      ...packs.map((p) => ({ face: { name: p.name, hue }, label: p.name, hint: p.id, onSelect: () => act({ type: "invite", ref: p.id }) })),
      packs.length ? null : { label: "所有角色都在对话里了", disabled: true },
      { icon: "plus", label: "持久化实例…", sub: "<角色>:<标签>，笔记跨会话", onSelect: inviteInstance },
      guests.length ? "sep" : null,
      guests.length ? { title: "移出对话" } : null,
      ...guests.map((m) => ({ icon: "userMinus", label: m.name, hint: m.ref, onSelect: () => act({ type: "kick", ref: m.ref }) })),
    ], { align: "end" });
  }

  async function inviteInstance() {
    const ref = (await dialog({
      title: "拉一个持久化实例进来",
      body: "写成 <角色>:<标签>，比如 dafeiyu:cafe。实例有自己的笔记，跨它参加过的所有会话；还没有的先用 /instance 创建。",
      input: { placeholder: "角色:标签" }, ok: "拉进来",
    }))?.trim();
    if (ref) act({ type: "invite", ref });
  }

  function moreMenu(anchor) {
    openMenu(anchor, [
      { icon: "plus", label: "新会话", hint: "/new", onSelect: () => act({ type: "new" }) },
      { icon: "refresh", label: "让最后说话的角色重说", hint: "/retry", onSelect: () => act({ type: "retry" }) },
      { icon: "undo", label: "删掉最后一条消息", hint: "/undo", onSelect: () => act({ type: "undo", n: 1 }) },
      "sep",
      { icon: "download", label: "导出 Markdown", onSelect: exportMarkdown },
      { icon: "book", label: "重新读取角色包", hint: "/reload", onSelect: () => act({ type: "reload" }) },
    ], { align: "end" });
  }

  function renderBusy() {
    $("#send").hidden = S.busy;
    $("#stop").hidden = !S.busy;
  }

  // --- messages -----------------------------------------------------------------------

  const thread = $("#thread");
  const fromBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight;
  const scrollToBottom = (smooth = false) => thread.scrollTo({ top: thread.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  const isSpeech = (m) => m && m.recalls == null && (m.kind === "me" || m.kind === "character" || m.kind === "person");
  const sameSpeaker = (a, b) => a.kind === b.kind && (a.kind === "me" || (a.ref || a.sender) === (b.ref || b.sender));
  const parts = (text) => text.split(/\n\s*\n/).filter((t) => t.trim());
  const oneLine = (text) => text.replace(/\s*\n\s*/g, " ");

  function renderMessages(stick = false, animate = true) {
    const box = $("#messages");
    const atBottom = stick || fromBottom() < 120;
    S.animate = animate;
    const out = [];
    if (!TOKEN) {
      out.push(h("div", { class: "empty" }, icon("warn"), h("strong", {}, "地址里缺少 token"),
        h("p", {}, "用 rhine web 启动时打印出来的链接打开这个页面。")));
    } else if (!S.state) {
      out.push(h("div", { class: "empty" }, h("i", { class: "spin" }), h("p", {}, "正在连接…")));
    } else if (!S.chat) {
      out.push(h("div", { class: "empty" }, icon("ghost"), h("strong", {}, "还没有角色"),
        h("p", {}, "先在命令行里用 rhine new <ID> 创建一个，再刷新这个页面。")));
    } else {
      const gone = new Set(S.messages.filter((m) => m.recalls != null).map((m) => m.recalls));
      let lastTs = 0, prev = null;
      for (const m of S.messages) {
        if (gone.has(m.id) && m.recalls == null) continue; // recalled: gone, as in a chat app
        if (m.ts - lastTs > 5 * 60 * 1000) { out.push(h("div", { class: "when" }, m.time)); prev = null; }
        lastTs = m.ts;
        const cont = isSpeech(m) && prev && sameSpeaker(prev, m) && m.ts - prev.ts < 3 * 60 * 1000;
        out.push(messageView(m, cont));
        prev = isSpeech(m) ? m : null;
        const after = S.notes[keyOf(m)]?.after || [];
        for (const line of after) out.push(traceView(line.text, line.icon, m.kind === "me" ? "after mine" : "after"));
        if (after.length) prev = null;
      }
      if (!S.messages.length && !S.live) out.push(helloView());
    }
    box.replaceChildren(...out);
    if (S.live) box.append(liveView(false));
    S.animate = true;
    if (atBottom) scrollToBottom();
    S.stuck = atBottom;
  }

  function renderLive() {
    const box = $("#messages");
    const atBottom = fromBottom() < 120;
    const old = $(".msg.live", box);
    if (S.live) {
      const el = liveView(!old);
      if (old) old.replaceWith(el);
      else { $(".hello", box)?.remove(); box.append(el); }
      $(".thinking", el)?.scrollTo(0, 1e9);
    } else old?.remove();
    if (atBottom) scrollToBottom();
  }

  function helloView() {
    const c = S.chat.character;
    return h("div", { class: "hello" }, face(c.name, HUES[0], 72), h("h2", {}, c.name), c.identity ? h("p", {}, c.identity) : null,
      h("p", { class: "faint" }, S.chat.kind === "group" ? "人多的时候，每个角色自己判断接不接话" : "打个招呼吧"));
  }

  function toolIcon(text) {
    if (/记下了|改了笔记|删了笔记/.test(text)) return "feather";
    if (/去查了/.test(text)) return "search";
    if (/翻聊天记录/.test(text)) return "history";
    if (/读了长消息/.test(text)) return "book";
    return "wand";
  }

  function traceView(text, name = toolIcon(text), cls = "") {
    return h("div", { class: cls ? `trace ${cls}` : "trace" }, icon(name), h("span", {}, text));
  }

  function thinkView(text, open, label = "思考", onToggle = null) {
    const el = h("details", { class: "think", open }, h("summary", {}, icon("right"), label), h("div", { class: "thinking" }, text));
    if (onToggle) el.addEventListener("toggle", () => onToggle(el.open));
    return el;
  }

  function messageView(m, cont) {
    if (m.recalls != null) {
      return h("div", { class: "notice", title: m.quote ? `撤回的是：${m.quote.text}` : null }, icon("undo"),
        m.kind === "me" ? "你撤回了一条消息" : `「${m.who}」撤回了一条消息`);
    }
    if (m.kind === "system") return h("div", { class: "notice" }, m.text);
    if (m.kind === "instruction") return h("div", { class: "notice instruction" }, icon("wand"), `系统指令：${m.text}`);
    if (m.kind === "lookup") {
      return h("details", { class: "lookup" }, h("summary", {}, icon("search"), `${S.chat.character.name}查到的东西`, icon("right", "chev")),
        h("div", { class: "lookup-body" }, m.text));
    }
    const mine = m.kind === "me";
    const n = S.notes[keyOf(m)];
    const body = h("div", { class: "msg-body" });
    if (!mine && !cont && S.chat.kind === "group") {
      body.append(h("div", { class: "msg-name" }, m.who, m.bot ? h("span", { class: "tag boxed" }, "机器人") : null));
    }
    if (n?.thinking && S.showThinking) body.append(thinkView(n.thinking, false));
    const texts = m.kind === "character" ? parts(m.text) : [m.text];
    let first = true;
    texts.forEach((text, i) => {
      if (!text && !(i === 0 && m.quote)) return;
      const bubble = h("div", { class: first ? "bubble first" : "bubble" });
      first = false;
      if (i === 0 && m.quote) bubble.append(h("div", { class: "quote" }, h("b", {}, m.quote.who), "：", oneLine(m.quote.text)));
      if (text) bubble.append(text);
      // Fingers can't point at a message: a tap brings up what can be done with it.
      if (m.id > 0) bubble.addEventListener("click", (e) => { if (TOUCH.matches) openMenu(e.currentTarget, messageActions(m)); });
      body.append(bubble);
    });
    for (const a of m.attachments || []) body.append(attachmentView(a));
    for (const t of n?.tools || []) body.append(traceView(t));
    if (n?.stop === "refusal") body.append(traceView("模型拒绝了这次回复", "warn"));
    if (n?.stop === "max_tokens") body.append(traceView("回复被 max_tokens 截断了", "warn"));
    if (m.pending) body.append(h("div", { class: "trace" }, "发送中…"));
    if (m.id > 0) body.append(toolsView(m));
    const key = keyOf(m);
    const cls = ["msg", m.kind, cont && "cont", m.pending && "pending", S.animate && !S.seen.has(key) && "new"].filter(Boolean).join(" ");
    S.seen.add(key);
    if (mine) return h("div", { class: cls, "data-id": m.id }, body);
    const who = face(m.who, hueOf(m), 36);
    if (m.kind === "character") {
      who.title = "双击拍一拍";
      who.addEventListener("dblclick", () => act({ type: "pat", ref: m.ref }));
    }
    return h("div", { class: cls, "data-id": m.id }, who, body);
  }

  function attachmentView(a) {
    const label = a.kind === "sticker" ? "表情包" : "图片";
    if (!a.url) {
      return h("div", { class: `media ${a.kind}` }, h("span", { class: "missing" }, `[${label}${a.hint ? "：" + a.hint : "（没能加载）"}]`));
    }
    const src = a.local ? a.url : `${a.url}?t=${encodeURIComponent(TOKEN)}`;
    const seen = a.description ? `看到的是：${a.description}` : a.hint;
    const img = h("img", { src, alt: a.description || a.hint || label, loading: "lazy", onclick: () => lightbox(src, seen) });
    img.addEventListener("load", () => { if (S.stuck) scrollToBottom(); });
    return h("div", { class: `media ${a.kind}`, title: seen || label }, img);
  }

  function messageActions(m) {
    const items = [{ icon: "quote", label: "引用", onSelect: () => setQuote(m) }];
    if (m.kind === "me") items.push({ icon: "undo", label: "撤回", onSelect: () => act({ type: "recall", id: m.id }) });
    if (m.kind === "character") {
      items.push({ icon: "hand", label: "拍一拍", onSelect: () => act({ type: "pat", ref: m.ref }) });
      if (m.id === lastMessage()?.id) items.push({ icon: "refresh", label: "重说", onSelect: () => act({ type: "retry" }) });
    }
    if (m.text) items.push({ icon: "copy", label: "复制", onSelect: () => copyText(m.text) });
    items.push("sep", { icon: "history", label: "回到这条之前", danger: true, onSelect: () => rewindTo(m) });
    return items;
  }

  function toolsView(m) {
    const bar = h("div", { class: "msg-tools", role: "toolbar", "aria-label": "消息操作" });
    for (const item of messageActions(m)) {
      if (item === "sep") continue;
      bar.append(h("button", { class: item.danger ? "btn icon sm danger" : "btn icon sm", type: "button", "aria-label": item.label,
        "data-tip": item.label, onclick: item.onSelect }, icon(item.icon)));
    }
    return bar;
  }

  async function rewindTo(m) {
    const after = S.messages.filter((x) => x.id >= m.id && x.id > 0).length;
    const ok = await dialog({
      title: "回到这条消息之前？",
      body: `这条和之后的 ${after} 条消息会删掉${m.kind === "me" ? "，你说的这句放回输入框，改了再发" : ""}。`,
      ok: "回到这里", danger: true,
    });
    if (ok) act({ type: "rewind", id: m.id });
  }

  function liveView(entering) {
    const l = S.live;
    const body = h("div", { class: "msg-body" });
    if (S.chat?.kind === "group") body.append(h("div", { class: "msg-name" }, l.name));
    if (l.thinking && S.showThinking) {
      const label = l.text ? "思考" : h("span", { class: "shimmer" }, "思考中");
      body.append(thinkView(l.thinking, l.thinkOpen ?? !l.text, label, (open) => { l.thinkOpen = open; }));
    }
    for (const t of l.tools) body.append(traceView(t));
    if (l.text) {
      parts(l.text).forEach((part, i) => body.append(h("div", { class: i ? "bubble" : "bubble first" }, part)));
    } else {
      body.append(h("div", { class: "bubble first" }, l.status === "looking"
        ? h("span", { class: "shimmer" }, `${l.name}在查…`)
        : h("span", { class: "typing", title: `${l.name}正在输入` }, h("i"), h("i"), h("i"))));
    }
    const who = face(l.name, hueOf({ kind: "character", ref: l.ref, who: l.name }), 36);
    return h("div", { class: entering ? "msg character live new" : "msg character live" }, who, body);
  }

  // --- composer -----------------------------------------------------------------------

  const input = $("#input");

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
    $("#send").classList.toggle("ready", !!(input.value.trim() || S.staged.length));
  }

  function act(frame) {
    if (S.busy && frame.type !== "stop") { toast(BUSY, "info"); return false; }
    return send(frame);
  }

  function setQuote(m) {
    const text = m.text || (m.attachments?.length ? `[${m.attachments[0].kind === "sticker" ? "表情包" : "图片"}]` : "");
    S.quote = { id: m.id, who: m.who, text };
    renderComposer();
    input.focus();
  }

  function renderComposer() {
    $("#quote-bar").hidden = !S.quote;
    if (S.quote) $("#quote-text").textContent = `回复 ${S.quote.who}：${oneLine(S.quote.text)}`;
    const staged = $("#staged");
    staged.hidden = !S.staged.length;
    staged.replaceChildren(...S.staged.map((s, i) => h("span", { class: "thumb" }, h("img", { src: s.preview, alt: "要发的图片" }),
      h("button", { class: "x", type: "button", "aria-label": "不发了", onclick: () => { S.staged.splice(i, 1); renderComposer(); } }, icon("x")))));
    autosize();
  }

  function echo(text, media) {
    // Shown at once; the history the server sends next replaces it.
    S.messages.push({ id: -Date.now(), ts: Date.now(), time: "", kind: "me", who: S.chat.me.nickname, sender: S.chat.me.id,
      text, quote: S.quote ? { who: S.quote.who, text: S.quote.text } : null,
      attachments: media.map((s) => ({ kind: s.kind, url: s.preview, local: true, hint: s.hint })), pending: true });
    renderMessages(true);
  }

  function submit() {
    if (!S.chat) return;
    const text = input.value;
    if (text.trim().startsWith("/")) {
      if (runCommand(text.trim())) { input.value = ""; autosize(); hideSlash(); }
      return;
    }
    if (S.busy) { toast(BUSY, "info"); return; }
    if (!text.trim() && !S.staged.length) {
      if (S.chat.kind === "group") act({ type: "nudge" }); // in a group, an empty send lets them go on
      return;
    }
    const media = S.staged.slice();
    if (!send({ type: "say", text, quote: S.quote?.id ?? null,
                media: media.map((s) => ({ kind: s.kind, data: s.data, hint: s.hint })) })) return;
    echo(text, media);
    input.value = ""; S.quote = null; S.staged = [];
    renderComposer();
  }

  function readBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function stage(file, kind) {
    if (!file || !file.type.startsWith("image/")) return;
    if (file.size > 10 * 1024 * 1024) { toast("图片太大了，最多 10 MB", "error"); return; }
    const item = { kind, data: await readBase64(file), preview: URL.createObjectURL(file), hint: null };
    if (kind === "sticker") {
      if (S.busy) { toast(BUSY, "info"); return; }
      const name = await dialog({ title: "发一个表情包", body: "给它起个名字，相当于聊天软件里表情包的描述；可以不填。",
        input: { placeholder: "比如：猫猫探头" }, ok: "发送" });
      if (name === null) return;
      item.hint = name.trim() || null;
      if (send({ type: "say", text: "", media: [{ kind, data: item.data, hint: item.hint }] })) echo("", [item]);
      return;
    }
    S.staged.push(item);
    renderComposer();
  }

  // --- slash commands -----------------------------------------------------------------

  const COMMANDS = [
    { name: "/system", usage: "<文字>", icon: "wand", desc: "给角色一条系统指令：照着做，但不会提起", run: (a) => act({ type: "system", text: a }) },
    { name: "/scene", usage: "[文字]", icon: "film", desc: "加一段情景：这段聊天之前发生过的事",
      run: (a) => (a ? send({ type: "scene", text: a }) : true) && openPanel("scenes") },
    { name: "/retry", usage: "", icon: "refresh", desc: "让最后说话的角色重说一遍", run: () => act({ type: "retry" }) },
    { name: "/undo", usage: "[n]", icon: "undo", desc: "删掉最后 n 条消息，默认 1 条", run: (a) => act({ type: "undo", n: parseInt(a, 10) || 1 }) },
    { name: "/recall", alias: "/撤回", usage: "", icon: "undo", desc: "撤回自己的最后一条消息", run: () => act({ type: "recall" }) },
    { name: "/pat", alias: "/拍一拍", usage: "[角色]", icon: "hand", desc: "拍一拍角色", run: (a) => act({ type: "pat", ref: a || null }) },
    { name: "/invite", usage: "<角色>[:<标签>]", icon: "userPlus", desc: "把另一个角色拉进对话", run: (a) => act({ type: "invite", ref: a }) },
    { name: "/kick", usage: "<角色>", icon: "userMinus", desc: "把拉进来的角色移出对话", run: (a) => act({ type: "kick", ref: a }) },
    { name: "/instance", usage: "<角色>:<标签>", icon: "plus", desc: "创建持久化实例：有自己的笔记，跨会话", run: (a) => send({ type: "instance", ref: a }) },
    { name: "/new", alias: "/clear", usage: "", icon: "plus", desc: "开一个新会话，旧的还在", run: () => act({ type: "new" }) },
    { name: "/name", usage: "<昵称>", icon: "user", desc: "改你的昵称，还是同一个人", run: (a) => identity(S.chat.me.user, a) },
    { name: "/as", usage: "<ID> [昵称]", icon: "users", desc: "换成另一个人发言",
      run: (a) => { const [u, ...n] = a.split(/\s+/); return identity(u, n.join(" ") || u); } },
    { name: "/model", usage: "[模型]", icon: "sparkles", desc: "这个页面换一个回复模型；不写时回到配置里的", run: (a) => send({ type: "model", name: a }) },
    { name: "/reload", usage: "", icon: "book", desc: "重新读取角色包", run: () => act({ type: "reload" }) },
    { name: "/export", usage: "", icon: "download", desc: "把本会话导出成 Markdown", run: () => exportMarkdown() },
  ];

  function runCommand(line) {
    const head = line.split(/\s+/, 1)[0];
    const arg = line.slice(head.length).trim();
    const cmd = COMMANDS.find((c) => c.name === head || c.alias === head);
    if (!cmd) { toast(`没有 ${head} 这个命令`, "error"); return false; }
    if (cmd.usage.startsWith("<") && !arg) { toast(`用法：${cmd.name} ${cmd.usage}`, "error"); return false; }
    return cmd.run(arg) !== false;
  }

  function updateSlash() {
    const text = input.value;
    if (!S.chat || !text.startsWith("/") || /\s/.test(text)) { hideSlash(); return; }
    S.slash = COMMANDS.filter((c) => c.name.startsWith(text) || c.alias?.startsWith(text));
    if (!S.slash.length) { hideSlash(); return; }
    S.slashAt = Math.min(S.slashAt, S.slash.length - 1);
    const box = $("#slash");
    box.hidden = false;
    box.replaceChildren(
      h("div", { class: "slash-list", role: "listbox" }, S.slash.map((c, i) => h("button", {
        class: "slash-item", type: "button", role: "option", "data-active": String(i === S.slashAt),
        onmousedown: (e) => { e.preventDefault(); pickSlash(i); },
        onpointermove: (e) => { if (e.pointerType === "mouse" && S.slashAt !== i) { S.slashAt = i; markSlash(); } },
      }, h("b", {}, c.name, c.usage ? h("small", {}, ` ${c.usage}`) : null), h("span", {}, c.desc),
      h("span", { class: "alias" }, c.alias || "")))),
      h("div", { class: "list-foot" }, h("span", {}, kbd("↑"), kbd("↓"), " 选择"), h("span", {}, kbd("Tab"), " 补全"),
        h("span", {}, kbd("Enter"), " 运行"), h("span", {}, kbd("Esc"), " 关闭")));
    markSlash();
  }

  function markSlash() {
    $$(".slash-item", $("#slash")).forEach((el, i) => { el.dataset.active = String(i === S.slashAt); });
    $('#slash [data-active="true"]')?.scrollIntoView({ block: "nearest" });
  }

  function hideSlash() { $("#slash").hidden = true; S.slash = []; S.slashAt = 0; }

  function pickSlash(i) {
    const c = S.slash[i];
    if (!c) return;
    input.value = c.name + (c.usage ? " " : "");
    hideSlash(); autosize(); input.focus();
  }

  function identity(user, nickname) {
    if (!user) return false;
    saved.set("user", user); saved.set("nickname", nickname);
    return send({ type: "identity", user, nickname });
  }

  function exportMarkdown() {
    if (!S.chat) return false;
    const gone = new Set(S.messages.filter((m) => m.recalls != null).map((m) => m.recalls));
    const lines = [`# ${S.chat.label}`, "", `会话 ${S.chat.id}`, ""];
    for (const m of S.messages) {
      if (m.id < 0 || gone.has(m.id) && m.recalls == null) continue;
      const when = new Date(m.ts).toLocaleString();
      let text = m.recalls != null ? "（撤回了一条消息）" : m.text;
      if (m.quote) text = `> ${m.quote.who}：${m.quote.text}\n\n${text}`;
      for (const a of m.attachments || []) {
        text += ` [${a.kind === "sticker" ? "表情包" : "图片"}${a.description || a.hint ? "：" + (a.description || a.hint) : ""}]`;
      }
      lines.push(`**${m.kind === "system" ? "系统" : m.who}** · ${when}  \n${text.replace(/\n/g, "  \n")}\n`);
    }
    const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `${S.chat.id.replace(/[:/]/g, "-")}.md` });
    document.body.append(a); a.click(); a.remove();
    return true;
  }

  // --- the command palette (Ctrl K) ---------------------------------------------------

  let palette = null;

  function paletteItems() {
    const c = S.chat;
    const out = [];
    if (c) {
      for (const cmd of COMMANDS) {
        out.push({ group: "命令", icon: cmd.icon, title: cmd.usage ? `${cmd.name} ${cmd.usage}` : cmd.name, sub: cmd.desc, keys: cmd.alias,
          run: () => {
            if (cmd.usage.startsWith("<")) { input.value = cmd.name + " "; autosize(); input.focus(); return; }
            runCommand(cmd.name);
          } });
      }
      for (const [what, [title, ic]] of Object.entries(PANELS)) out.push({ group: "面板", icon: ic, title, run: () => openPanel(what) });
    }
    for (const p of S.state?.characters || []) {
      out.push({ group: "角色", face: p.name, title: p.name, sub: `${p.id} · ${p.identity}`, hint: p.id === c?.character.ref ? "当前" : "",
        run: () => { if (p.id !== c?.character.ref) openCharacter(p.id); } });
    }
    for (const s of c?.sessions || []) {
      out.push({ group: "会话", icon: "chat", title: s.label, sub: s.preview || "（还没有消息）", hint: s.id === c.id ? "当前" : s.time,
        run: () => { if (s.id !== c.id) send({ type: "resume", id: s.id }); } });
    }
    for (const t of THEMES) {
      out.push({ group: "外观", icon: t.icon, title: `主题：${t.label}`, hint: S.theme === t.id ? "当前" : "", run: () => setTheme(t.id) });
    }
    out.push({ group: "外观", icon: "eye", title: S.showThinking ? "隐藏思考摘要" : "显示思考摘要", run: () => setThinking(!S.showThinking) });
    return out;
  }

  function highlight(text, needle) {
    const at = needle ? text.toLowerCase().indexOf(needle) : -1;
    return at < 0 ? text : [text.slice(0, at), h("mark", {}, text.slice(at, at + needle.length)), text.slice(at + needle.length)];
  }

  function togglePalette() {
    if (palette) palette.close();
    else openPalette();
  }

  function openPalette() {
    closeMenu();
    const all = paletteItems();
    let active = 0, flat = [];
    const field = h("input", { type: "text", placeholder: "搜索命令、角色、会话和面板", autocomplete: "off", spellcheck: "false", "aria-label": "搜索" });
    const list = h("div", { class: "palette-list", role: "listbox" });
    const count = h("span", {});
    const close = () => {
      scrim.remove(); palette = null;
      removeEventListener("keydown", keys, true);
      if (document.activeElement === document.body) input.focus({ preventScroll: true });
    };
    const run = (item) => { close(); item?.run(); };
    const scrim = h("div", { class: "scrim", onmousedown: (e) => { if (e.target === scrim) close(); } },
      h("div", { class: "palette", role: "dialog", "aria-modal": "true", "aria-label": "命令面板" },
        h("div", { class: "palette-input" }, icon("search"), field, h("button", { class: "btn sm", type: "button", "aria-label": "关闭", onclick: close }, kbd("Esc"))),
        list,
        h("div", { class: "list-foot" }, h("span", {}, kbd("↑"), kbd("↓"), " 选择"), h("span", {}, kbd("Enter"), " 执行"),
          h("span", {}, kbd("Esc"), " 关闭"), h("span", { class: "grow" }), count)));
    const mark = () => {
      $$(".palette-item", list).forEach((el, i) => { el.dataset.active = String(i === active); });
      $('[data-active="true"]', list)?.scrollIntoView({ block: "nearest" });
    };
    const render = () => {
      const needle = field.value.trim().toLowerCase();
      const groups = new Map();
      for (const item of all) {
        if (needle && !`${item.title} ${item.sub || ""} ${item.keys || ""}`.toLowerCase().includes(needle)) continue;
        if (!groups.has(item.group)) groups.set(item.group, []);
        const g = groups.get(item.group);
        if (!needle && g.length >= 8) continue; // the rest are found by typing
        g.push(item);
      }
      flat = [...groups.values()].flat();
      active = Math.min(active, Math.max(0, flat.length - 1));
      let at = -1;
      list.replaceChildren(...(flat.length ? [...groups].map(([name, items]) => h("div", { class: "palette-group", role: "group", "aria-label": name },
        h("div", { class: "palette-group-name" }, name),
        items.map((item) => {
          const i = ++at;
          return h("button", { class: "palette-item", type: "button", role: "option", "data-active": String(i === active),
            onclick: () => run(item), onpointermove: (e) => { if (e.pointerType === "mouse" && active !== i) { active = i; mark(); } } },
          item.face ? face(item.face, HUES[0], 30) : h("span", { class: "palette-icon" }, icon(item.icon)),
          h("span", { class: "grow" }, h("strong", {}, highlight(item.title, needle)), item.sub ? h("small", {}, item.sub) : null),
          item.hint ? h("span", { class: "hint" }, item.hint) : null, icon("arrowRight", "go"));
        }))) : [h("div", { class: "palette-none" }, icon("search"), h("span", {}, `没有和「${field.value.trim()}」相符的`))]));
      count.textContent = `${flat.length} 项`;
      mark();
    };
    const keys = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
      if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) { e.preventDefault(); active = flat.length ? (active + 1) % flat.length : 0; mark(); return; }
      if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) { e.preventDefault(); active = flat.length ? (active - 1 + flat.length) % flat.length : 0; mark(); return; }
      if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); run(flat[active]); }
    };
    field.addEventListener("input", () => { active = 0; render(); });
    addEventListener("keydown", keys, true);
    document.body.append(scrim);
    palette = { close };
    render();
    field.focus();
  }

  // --- panels ---------------------------------------------------------------------

  const PANELS = {
    memory: ["笔记", "feather"],
    scenes: ["情景", "film"],
    context: ["上下文", "gauge"],
    prompt: ["提示词", "code"],
    settings: ["设置", "settings"],
  };
  const TASKS = { roleplay: "回复", reply_judge: "群聊筛选", vision: "看图", summary: "摘要" };

  function openPanel(what) {
    S.panel = what;
    const [title, ic] = PANELS[what];
    $("#drawer").hidden = false;
    $("#app").setAttribute("data-drawer", "");
    $("#drawer-title").textContent = title;
    $("#drawer-icon").replaceChildren(icon(ic));
    for (const b of $$("#nav button")) {
      if (b.dataset.panel === what) b.setAttribute("aria-current", "true");
      else b.removeAttribute("aria-current");
    }
    $("#settings-btn").setAttribute("aria-pressed", String(what === "settings"));
    closeSide(); // one layer over the conversation at a time
    const body = $("#drawer-body");
    if (what === "settings") {
      renderSettings();
      if (S.chat) { send({ type: "inspect", what: "models" }); send({ type: "inspect", what: "stats" }); }
    } else if (!S.chat) {
      body.replaceChildren(h("div", { class: "none" }, "先选一个角色"));
    } else {
      body.replaceChildren(h("div", { class: "none inline" }, h("i", { class: "spin" }), "加载中…"));
      send({ type: "inspect", what });
    }
    return true;
  }

  function closePanel() {
    S.panel = null;
    $("#drawer").hidden = true;
    $("#app").removeAttribute("data-drawer");
    for (const b of $$("#nav button")) b.removeAttribute("aria-current");
    $("#settings-btn").setAttribute("aria-pressed", "false");
    syncLayer();
  }

  const togglePanel = (what) => (S.panel === what ? closePanel() : openPanel(what));

  function sec(title, mark, small, ...kids) {
    return h("section", { class: "sec" },
      h("div", { class: "sec-head" }, typeof mark === "string" ? icon(mark) : mark, h("span", {}, title), small ? h("small", {}, small) : null),
      ...kids);
  }

  function row(title, text, control) {
    return h("div", { class: "row" }, h("div", {}, h("strong", {}, title), text ? h("p", {}, text) : null), control);
  }

  function switchRow(title, text, on, change) {
    const sw = h("span", { class: "switch", role: "switch", "aria-checked": String(on), "aria-label": title });
    return h("div", { class: "row tappable", tabindex: "0", onclick: () => { on = !on; sw.setAttribute("aria-checked", String(on)); change(on); },
      onkeydown: (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); e.currentTarget.click(); } } },
    h("div", {}, h("strong", {}, title), text ? h("p", {}, text) : null), sw);
  }

  // Choices that exclude one another: one mark travels between them.
  function seg(options, value, change) {
    const row = h("div", { class: "seg", "data-slide": "" });
    for (const [id, label] of options) {
      row.append(h("button", { class: "chip", type: "button", "aria-pressed": String(id === value),
        onclick: (e) => { for (const b of row.children) b.setAttribute("aria-pressed", String(b === e.currentTarget)); change(id); } }, label));
    }
    return row;
  }

  function entry(n, text, about, remove) {
    return h("div", { class: "entry" }, h("span", { class: "n" }, n),
      h("span", { class: "txt" }, about ? h("span", { class: "about" }, about) : null, text),
      h("button", { class: "btn icon sm danger", type: "button", "aria-label": "删掉", "data-tip": "删掉", "data-tip-side": "end", onclick: remove }, icon("trash")));
  }

  function setThinking(on) {
    S.showThinking = on;
    saved.set("thinking", on);
    renderMessages();
  }

  function renderPanel(what, data) {
    if (what === "models" || what === "stats") {
      S[what] = data;
      if (S.panel === "settings") renderSettings();
      return;
    }
    if (what !== S.panel) return;
    const body = $("#drawer-body");
    if (what === "memory") {
      body.replaceChildren(...data.map((c, i) => sec(c.name, face(c.name, HUES[i % HUES.length], 22), c.instance ? `${c.ref} · 笔记跨会话` : c.ref,
        c.summary ? h("div", { class: "summary" }, h("span", { class: "label", style: "display: block" }, "对话摘要"), c.summary) : null,
        c.notes.length
          ? c.notes.map((m) => entry(m.id, m.text, m.about, () => forget(c, m)))
          : h("div", { class: "none" }, "还没有笔记：角色会自己记下要记住的事。"))));
    } else if (what === "scenes") {
      const area = h("textarea", { class: "textarea", rows: "3", placeholder: "这段聊天之前发生过的事，比如：昨晚你们一起看了烟花" });
      const add = () => { const text = area.value.trim(); if (text) send({ type: "scene", text }); };
      body.replaceChildren(
        sec("这个会话的情景", "film", data.length ? `${data.length} 段` : null,
          h("p", { class: "sec-note" }, "角色把情景当作事实，按自己的性格去理解。只对这个会话有效。"),
          data.length ? data.map((s) => entry(s.id, s.text, null, () => send({ type: "unscene", id: s.id })))
            : h("div", { class: "none" }, "还没有情景")),
        sec("加一段", "plus", null, h("div", { class: "fields" }, area,
          h("div", { class: "inline" }, h("span", { class: "grow" }), h("button", { class: "btn primary", type: "button", onclick: add }, "添加")))));
    } else if (what === "context") {
      const total = data.sections.reduce((n, p) => n + p.size, 0) + data.history.size || 1;
      const meter = (label, size, strong) => h("div", { class: strong ? "meter-row strong" : "meter-row" }, h("span", {}, label),
        h("span", { class: "v" }, size.toLocaleString()), h("div", { class: "meter" }, h("i", { style: `width: ${(size / total * 100).toFixed(1)}%` })));
      body.replaceChildren(
        sec("整个请求", "gauge", data.total != null ? `${data.total.toLocaleString()} tokens` : null,
          h("p", { class: "sec-note" }, data.total != null ? "下面各部分的单位是 token。" : "这个接口不能计数，下面是字符数。")),
        sec("system prompt", "code", null, data.sections.map((p) => meter(p.label, p.size))),
        sec("聊天记录", "chat", `${data.history.in_window}/${data.history.total} 条`, meter("在上下文里的消息", data.history.size, true),
          h("p", { class: "sec-note" }, `历史预算约 ${data.budget.toLocaleString()} token（config.yaml 的 history_tokens），更早的由对话摘要接手。`)));
    } else if (what === "prompt") {
      body.replaceChildren(sec("system prompt", "code", `${data.system.length.toLocaleString()} 字`,
        h("div", { class: "inline" }, h("p", { class: "sec-note grow" }, "此刻发给模型的完整提示词。"),
          h("button", { class: "btn sm", type: "button", onclick: () => copyText(data.system) }, icon("copy"), "复制")),
        h("pre", { class: "prompt-text" }, data.system)));
    }
  }

  async function forget(c, m) {
    const ok = await dialog({ title: "删掉这条笔记？", body: "删掉的还能用 rhine memory restore 恢复。", ok: "删掉", danger: true });
    if (ok) send({ type: "forget", id: m.id, ref: c.ref });
  }

  function renderSettings() {
    if (S.panel !== "settings") return;
    const body = $("#drawer-body");
    const scroll = body.scrollTop;
    const c = S.chat;
    const secs = [sec("外观", "palette", null,
      row("主题", null, seg(THEMES.map((t) => [t.id, t.label]), S.theme, (id) => setTheme(id, true))),
      row("深色用", "跟随系统、系统是深色时用哪一种。", seg([["graphite", "石墨"], ["ink", "墨黑"]], S.dark, setDark)),
      switchRow("显示思考摘要", "角色回复之前想了什么，可以展开看。", S.showThinking, setThinking))];
    if (c) {
      const user = h("input", { class: "input", value: c.me.user, "aria-label": "ID" });
      const nick = h("input", { class: "input", value: c.me.nickname, "aria-label": "昵称" });
      const apply = () => identity(user.value.trim(), nick.value.trim() || user.value.trim());
      secs.push(sec("你", "user", null,
        h("div", { class: "fields" },
          h("label", { class: "field" }, h("span", { class: "label" }, "ID"), user),
          h("label", { class: "field" }, h("span", { class: "label" }, "昵称"), nick)),
        h("div", { class: "inline" }, h("p", { class: "sec-note grow" }, "改昵称还是同一个人；换一个 ID 就是另一个人在说话。"),
          h("button", { class: "btn primary", type: "button", onclick: apply }, "应用"))));
      const models = S.models?.catalog || [];
      const pick = h("select", { class: "select", "aria-label": "回复模型" }, h("option", { value: "" }, "用配置里的"),
        models.map((m) => h("option", { value: m.id, selected: m.id === S.models?.override }, `${m.id}（${m.vendor}）`)));
      pick.addEventListener("change", () => send({ type: "model", name: pick.value }));
      const reply = row("回复", `现在用 ${c.model}。只在这个页面里换，config.yaml 不变。`, pick);
      reply.classList.add("stack");
      secs.push(sec("模型", "sparkles", null, reply,
        (S.models?.tasks || []).filter((t) => t.task !== "roleplay").map((t) => row(TASKS[t.task] || t.task, null,
          h("span", { class: "mono faint" }, t.error ? "配置有误" : t.model)))));
      if (c.judge.available) {
        secs.push(sec("群聊", "users", null, switchRow("群聊筛选", "人多时，没人 @ 的消息先由小模型判断角色会不会开口。", c.judge.on,
          (on) => send({ type: "judge", on }))));
      }
      const packs = (S.state.characters || []).filter((p) => !c.cast.some((x) => x.ref.split(":")[0] === p.id));
      const who = h("input", { class: "input", placeholder: "角色，或 <角色>:<标签>", list: "packs", "aria-label": "拉进来的角色" });
      secs.push(sec("对话里的角色", "users", `${c.cast.length} 个`,
        c.cast.map((m, i) => h("div", { class: "row" },
          h("span", { class: "who" }, face(m.name, HUES[i % HUES.length], 28), h("span", { class: "grow" }, h("strong", {}, m.name), h("small", {}, m.ref))),
          m.first ? h("span", { class: "tag" }, "最初的角色") : h("button", { class: "btn sm danger", type: "button", onclick: () => act({ type: "kick", ref: m.ref }) }, "移出"))),
        h("div", { class: "row stack" }, h("datalist", { id: "packs" }, packs.map((p) => h("option", { value: p.id }, p.name))),
          h("div", { class: "inline" }, who, h("button", { class: "btn primary", type: "button", onclick: () => who.value.trim() && act({ type: "invite", ref: who.value.trim() }) }, "拉进来")),
          h("p", {}, "人一多，每个角色都像在群里一样自己判断接不接话。"))));
      const st = S.stats;
      const n = (v) => (v || 0).toLocaleString();
      secs.push(sec("这次运行", "gauge", null,
        st ? h("dl", { class: "kv" }, h("dt", {}, "调用"), h("dd", {}, `${st.calls} 次`), h("dt", {}, "输入 token"), h("dd", {}, n(st.tokens.input)),
          h("dt", {}, "缓存读"), h("dd", {}, n(st.tokens.cache_read)), h("dt", {}, "输出 token"), h("dd", {}, n(st.tokens.output)),
          h("dt", {}, "估算费用"), h("dd", {}, `$${st.cost.toFixed(4)}`)) : h("div", { class: "none" }, "加载中…"),
        h("div", { class: "inline" }, h("button", { class: "btn", type: "button", onclick: () => act({ type: "reload" }) }, icon("book"), "重新读取角色包"),
          h("button", { class: "btn", type: "button", onclick: exportMarkdown }, icon("download"), "导出 Markdown"))));
    }
    if (!TOUCH.matches) {
      const keys = [[["Enter"], "发送"], [["Shift", "Enter"], "换行"], [["/"], "命令；在输入框外按也行"], [[MOD, "K"], "命令面板：命令、角色、会话、面板"],
        [["Esc"], "关掉菜单和面板；不引用了"], [["↑", "↓"], "在命令列表里选"]];
      secs.push(sec("快捷键", "keyboard", null, h("table", { class: "keys" },
        h("tbody", {}, keys.map(([k, what]) => h("tr", {}, h("td", {}, k.map(kbd)), h("td", {}, what)))))));
    }
    body.replaceChildren(...secs);
    body.scrollTop = scroll;
  }

  // --- narrow screens: the sessions over the conversation ---------------------------------

  function openSide() { $("#app").setAttribute("data-side", ""); syncLayer(); }
  function closeSide() { $("#app").removeAttribute("data-side"); syncLayer(); }

  function syncLayer() {
    const side = NARROW.matches && $("#app").hasAttribute("data-side");
    const sheet = PHONE.matches && !$("#drawer").hidden;
    $("#layer-scrim").hidden = !side && !sheet;
  }

  NARROW.addEventListener("change", closeSide);
  PHONE.addEventListener("change", syncLayer);

  // --- wiring -------------------------------------------------------------------------

  input.addEventListener("keydown", (e) => {
    const enter = e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229;
    if (!$("#slash").hidden && S.slash.length) {
      const c = S.slash[S.slashAt];
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        S.slashAt = (S.slashAt + (e.key === "ArrowDown" ? 1 : -1) + S.slash.length) % S.slash.length;
        markSlash(); return;
      }
      if (e.key === "Tab") { e.preventDefault(); pickSlash(S.slashAt); return; }
      if (e.key === "Escape") { e.preventDefault(); hideSlash(); return; }
      if (enter && input.value.trim() !== c.name) {
        // A command that needs words gets completed; one that doesn't runs at once.
        if (c.usage.startsWith("<")) { e.preventDefault(); pickSlash(S.slashAt); return; }
        input.value = c.name;
      }
    }
    if (enter) { e.preventDefault(); submit(); return; }
    if (e.key === "Escape" && S.quote) { S.quote = null; renderComposer(); }
  });
  input.addEventListener("input", () => { autosize(); updateSlash(); });
  input.addEventListener("blur", () => setTimeout(hideSlash, 150));
  input.addEventListener("paste", (e) => {
    for (const item of e.clipboardData?.items || []) {
      if (item.type.startsWith("image/")) { e.preventDefault(); stage(item.getAsFile(), "image"); }
    }
  });

  const chat = $("#chat");
  chat.addEventListener("dragover", (e) => { e.preventDefault(); $("#composer-box").classList.add("drop"); });
  chat.addEventListener("dragleave", (e) => { if (!chat.contains(e.relatedTarget)) $("#composer-box").classList.remove("drop"); });
  chat.addEventListener("drop", (e) => {
    e.preventDefault();
    $("#composer-box").classList.remove("drop");
    for (const file of e.dataTransfer?.files || []) stage(file, "image");
  });

  thread.addEventListener("scroll", () => {
    S.stuck = fromBottom() < 120;
    $("#jump").hidden = fromBottom() < 400;
  }, { passive: true });
  $("#jump").addEventListener("click", () => scrollToBottom(true));

  $("#send").addEventListener("click", submit);
  $("#stop").addEventListener("click", () => send({ type: "stop" }));
  $("#quote-cancel").addEventListener("click", () => { S.quote = null; renderComposer(); input.focus(); });
  $("#attach-image").addEventListener("click", () => $("#file-image").click());
  $("#attach-sticker").addEventListener("click", () => $("#file-sticker").click());
  $("#file-image").addEventListener("change", (e) => { for (const f of e.target.files) stage(f, "image"); e.target.value = ""; });
  $("#file-sticker").addEventListener("change", (e) => { stage(e.target.files[0], "sticker"); e.target.value = ""; });
  $("#who-switch").addEventListener("click", (e) => characterMenu(e.currentTarget));
  $("#new-session").addEventListener("click", () => { act({ type: "new" }); closeSide(); });
  $("#me").addEventListener("click", () => { openPanel("settings"); closeSide(); });
  for (const b of $$("#nav button")) b.addEventListener("click", () => togglePanel(b.dataset.panel));
  $("#panels-btn").addEventListener("click", (e) => openMenu(e.currentTarget, [
    { title: "面板" },
    ...["memory", "scenes", "context", "prompt"].map((w) => ({ icon: PANELS[w][1], label: PANELS[w][0], hint: S.panel === w ? "打开着" : "", onSelect: () => togglePanel(w) })),
  ], { align: "end" }));
  $("#theme-btn").addEventListener("click", (e) => openMenu(e.currentTarget, [
    { title: "主题" },
    ...THEMES.map((t) => ({ icon: t.icon, label: t.label, hint: S.theme === t.id ? "当前" : "", onSelect: () => setTheme(t.id) })),
  ], { align: "end" }));
  $("#settings-btn").addEventListener("click", () => togglePanel("settings"));
  $("#drawer-close").addEventListener("click", closePanel);
  $("#open-side").addEventListener("click", () => ($("#app").hasAttribute("data-side") ? closeSide() : openSide()));
  $("#layer-scrim").addEventListener("click", () => { closeSide(); if (PHONE.matches) closePanel(); });
  $("#open-palette").addEventListener("click", togglePalette);

  addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); togglePalette(); return; }
    const typing = e.target instanceof Element && e.target.matches("input, textarea, select, [contenteditable]");
    if (e.key === "Escape" && !typing) {
      if (!$("#drawer").hidden) closePanel();
      else closeSide();
      return;
    }
    if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey && !palette) {
      e.preventDefault();
      input.focus();
      if (!input.value) { input.value = "/"; autosize(); updateSlash(); }
    }
  });

  $(".search-trigger .kbd").textContent = `${MOD} K`;
  $("#open-palette").dataset.tip = `命令 ${MOD} K`;
  applyTheme();
  renderMessages();
  connect();
})();
