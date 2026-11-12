// LuckyLook's conversation, shared by the popup and the app. Python sends answers as HTML it has already made safe
// (lucky.markdown escapes everything first); everything else here is set as text, never as HTML.
const LL = window.LL = {};
const $ = (s) => document.querySelector(s);
const api = () => window.pywebview.api;
let turn = null, timer = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function nearBottom(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < 120; }
function follow(log, stick) { if (stick) log.scrollTop = log.scrollHeight; }

LL.addTurn = function (q, html) {
  const log = $("#log");
  $(".empty")?.remove();
  $(".welcome")?.remove();
  const t = { el: el("section", "turn") };
  t.el.append(el("div", "q", q));
  t.steps = t.el.appendChild(el("div", "steps"));
  t.a = t.el.appendChild(el("div", "a"));
  t.meta = t.el.appendChild(el("div", "meta"));
  if (html != null) {
    t.a.innerHTML = html;
    copyButton(t, t.a.innerText);
  }
  log.append(t.el);
  return t;
};

LL.begin = function (q) {
  const log = $("#log");
  turn = LL.addTurn(q);
  turn.start = performance.now();
  turn.think = turn.el.insertBefore(el("div", "thinking"), turn.a);
  turn.label = turn.think.appendChild(el("span", "shimmer", "Thinking"));
  turn.clock = turn.think.appendChild(el("span", "timer", "0.0 s"));
  clearInterval(timer);
  timer = setInterval(() => { turn.clock.textContent = ((performance.now() - turn.start) / 1000).toFixed(1) + " s"; }, 100);
  document.body.classList.add("busy");
  log.scrollTop = log.scrollHeight;
};

LL.answer = function (html) {
  if (!turn) return;
  const log = $("#log"), stick = nearBottom(log);
  turn.label.textContent = "Writing";
  turn.a.innerHTML = html + '<span class="caret"></span>';
  follow(log, stick);
};

LL.step = function (label, detail) {
  if (!turn) return;
  const log = $("#log"), stick = nearBottom(log);
  const s = el("div", "step run");
  s.append(el("span", "dot"), el("span", "label", label), el("span", "detail", detail), el("span", "res"));
  turn.steps.append(s);
  turn.label.textContent = label + "…";
  follow(log, stick);
};

LL.stepDone = function (res) {
  const s = turn?.steps.lastElementChild;
  if (!s) return;
  s.className = "step ok";
  s.querySelector(".res").textContent = res;
};

LL.confirm = function (id, html) {
  if (!turn) return;
  const log = $("#log");
  const card = el("div", "ask");
  card.innerHTML = html;
  const row = card.appendChild(el("div", "buttons"));
  const yes = row.appendChild(el("button", "btn primary", "Allow"));
  const no = row.appendChild(el("button", "btn", "Deny"));
  const decide = (ok) => {
    api().decide(id, ok);
    row.remove();
    card.append(el("div", "verdict", ok ? "✓ Allowed" : "Not allowed"));
    turn && (turn.label.textContent = ok ? "Working" : "Thinking");
  };
  yes.onclick = () => decide(true);
  no.onclick = () => decide(false);
  turn.el.insertBefore(card, turn.a);
  turn.label.textContent = "Waiting for you to allow or deny";
  log.scrollTop = log.scrollHeight;
  yes.focus();
};

LL.done = function (html, meta, raw) {
  clearInterval(timer);
  document.body.classList.remove("busy");
  if (!turn) return;
  const log = $("#log"), stick = nearBottom(log);
  turn.think.remove();
  turn.steps.querySelectorAll(".run").forEach((s) => (s.className = "step"));  // stopped mid-step: no endless spinner
  turn.a.innerHTML = html;
  if (meta.note) turn.a.append(el("p", "hint", meta.note));
  turn.meta.append(el("span", "", meta.seconds + " s"));
  if (meta.steps.length) turn.meta.append(el("span", "", meta.steps.length + (meta.steps.length > 1 ? " steps" : " step")));
  copyButton(turn, raw);
  follow(log, stick);
  turn = null;
  LL.afterDone && LL.afterDone();
};

function copyButton(t, text) {
  const b = t.meta.appendChild(el("button", "", "Copy"));
  b.onclick = () => { api().copy(text); b.textContent = "Copied ✓"; setTimeout(() => (b.textContent = "Copy"), 1400); };
}

LL.fail = function (text) {
  if (turn) { turn.el.remove(); turn = null; }
  clearInterval(timer);
  document.body.classList.remove("busy");
  toast(text);
};

LL.clear = function () { $("#log").innerHTML = ""; LL.onClear && LL.onClear(); };
LL.frost = function (url) { $("#frost").style.backgroundImage = `url("${url}")`; };

function toast(text) {
  const t = document.body.appendChild(el("div", "toast", text));
  setTimeout(() => t.remove(), 3200);
}
LL.toast = toast;

// clicks inside answers: file chips open (⋯ or right-click shows them in Explorer), links open in Edge, code copies
document.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) return e.target.closest(".reveal") ? api().reveal(chip.dataset.path) : api().open(chip.dataset.path);
  const link = e.target.closest("a[data-url]");
  if (link) { e.preventDefault(); return api().url(link.dataset.url); }
  const copy = e.target.closest(".code .copy");
  if (copy) {
    api().copy(copy.parentElement.querySelector("pre").innerText);
    copy.textContent = "Copied ✓";
    setTimeout(() => (copy.textContent = "Copy"), 1400);
  }
});
document.addEventListener("contextmenu", (e) => {
  const chip = e.target.closest(".chip");
  e.preventDefault();
  if (chip) api().reveal(chip.dataset.path);
});

// the little numbers: the info strip (popup) and the At a glance panel (app) refresh every few seconds while shown
LL.gb = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + " TB" : n + " GB");
LL.count = (n) => (n == null ? "…" : n >= 1e6 ? (n / 1e6).toFixed(2) + " M" : n >= 1e4 ? Math.round(n / 1e3) + " k" : n.toLocaleString());
LL.ago = function (t) {
  if (!t) return "never";
  const m = Math.round((Date.now() / 1000 - t) / 60);
  return m < 1 ? "just now" : m < 60 ? m + " min ago" : m < 1440 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " d ago";
};
LL.until = function (t) {
  const m = Math.round((t - Date.now() / 1000) / 60);
  return m <= 0 ? "soon" : m < 60 ? "in " + m + " min" : "in " + Math.round(m / 60) + " h";
};
LL.modelText = function (m) {
  return m.state === "ready" ? `${m.name} · ${m.vram} GB on the graphics card` : m.state === "asleep" ? `${m.name} · asleep, wakes when you ask` : "Ollama isn't running";
};
let polling = null;
LL.poll = function (render, every = 3000) {
  const tick = () => window.pywebview && api().stats().then(render).catch(() => {});
  clearInterval(polling);
  tick();
  polling = setInterval(() => { if (!document.hidden) tick(); }, every);
};
LL.unpoll = () => clearInterval(polling);
