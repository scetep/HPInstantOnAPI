/*
 * Instant On network map — Home Assistant sidebar panel.
 *
 * Data comes from the `instant_on/topology/subscribe` websocket command and is
 * pushed after every integration refresh. Rendering is plain DOM + SVG so the
 * panel has no build step and no external dependencies.
 */

const CARD_W = 264;
const CHIP_W = 200;
const CHIP_H = 42;
const CHIP_GAP = 8;
const CHIPS_PER_COL = 8;
const GAP_X = 28;
const GAP_Y = 54;
const LANE_HEAD_H = 28;
const LANE_HEAD_GAP = 12;
const PORT_W = 17;
const PORT_H = 15;
const PORT_GAP = 3;
const PAD = 60;

const STORAGE_KEY = "instant-on-map";

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const speedLabel = (mbps) =>
  !mbps ? "" : mbps >= 1000 ? `${mbps % 1000 ? (mbps / 1000).toFixed(1) : mbps / 1000}G` : `${mbps}M`;

const speedClass = (mbps) =>
  !mbps ? "s-none" : mbps >= 2500 ? "s-multi" : mbps >= 1000 ? "s-gig" : mbps >= 100 ? "s-fast" : "s-slow";

const bandLabel = (band) => ({ "2.4ghz": "2.4 GHz", "5ghz": "5 GHz", "6ghz": "6 GHz" })[band] || band || "Wi-Fi";

const qualityIcon = (q) =>
  ({ good: "mdi:wifi-strength-4", fair: "mdi:wifi-strength-2", poor: "mdi:wifi-strength-1" })[q] || "mdi:wifi";

const bytes = (n) => {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

const bps = (n) => {
  if (n == null) return "–";
  const u = ["bps", "Kbps", "Mbps", "Gbps"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) {
    n /= 1000;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

const duration = (s) => {
  if (s == null) return "–";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};

const since = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString() : "–");

class InstantOnPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._siteId = null;
    this._selected = null;
    this._view = { x: 0, y: 0, k: 1 };
    this._fitted = false;
    this._pointers = new Map();
    this._opts = { wireless: true, wired: true, offline: false, query: "" };
    try {
      Object.assign(this._opts, JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"), { query: "" });
    } catch (_e) {
      /* storage unavailable */
    }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._renderShell();
      this._subscribe();
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) {
      menu.hass = hass;
      menu.narrow = this._narrow;
    }
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menu = this.shadowRoot?.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
    this.toggleAttribute("narrow", !!narrow);
  }

  set panel(_panel) {}

  connectedCallback() {
    if (this._hass && !this._unsub) this._subscribe();
    this._resizeObserver = new ResizeObserver(() => {
      if (!this._fitted) this._fit();
    });
    this._resizeObserver.observe(this);
  }

  disconnectedCallback() {
    this._unsub?.then((unsub) => unsub()).catch(() => {});
    this._unsub = undefined;
    this._resizeObserver?.disconnect();
  }

  _subscribe() {
    this._unsub = this._hass.connection.subscribeMessage(
      (msg) => {
        this._data = msg;
        if (!this._siteId || !msg.sites.some((s) => s.id === this._siteId)) {
          this._siteId = msg.sites[0]?.id ?? null;
        }
        this._render();
      },
      { type: "instant_on/topology/subscribe" },
    );
    this._unsub.catch((err) => {
      this._error = err?.message || String(err);
      this._render();
    });
  }

  _saveOpts() {
    try {
      const { query, ...rest } = this._opts;
      localStorage.setItem(STORAGE_KEY, JSON.stringify(rest));
    } catch (_e) {
      /* ignore */
    }
  }

  // ------------------------------------------------------------------ shell

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Instant On network</div>
        <select class="site" hidden></select>
        <div class="spacer"></div>
        <input class="search" type="search" placeholder="Search name, MAC or IP" />
      </div>
      <div class="statusbar"></div>
      <div class="controls">
        <button class="toggle" data-opt="wireless"><ha-icon icon="mdi:wifi"></ha-icon>Wireless</button>
        <button class="toggle" data-opt="wired"><ha-icon icon="mdi:ethernet"></ha-icon>Wired</button>
        <button class="toggle" data-opt="offline"><ha-icon icon="mdi:lan-disconnect"></ha-icon>Offline</button>
        <div class="spacer"></div>
        <button class="icon" data-zoom="out" title="Zoom out"><ha-icon icon="mdi:magnify-minus-outline"></ha-icon></button>
        <button class="icon" data-zoom="fit" title="Fit to screen"><ha-icon icon="mdi:fit-to-screen-outline"></ha-icon></button>
        <button class="icon" data-zoom="in" title="Zoom in"><ha-icon icon="mdi:magnify-plus-outline"></ha-icon></button>
      </div>
      <div class="viewport">
        <div class="stage"></div>
        <div class="empty">Waiting for Instant On data…</div>
        <div class="legend">
          <span><i class="sw s-multi"></i>2.5G+</span>
          <span><i class="sw s-gig"></i>1G</span>
          <span><i class="sw s-fast"></i>100M</span>
          <span><i class="sw s-slow"></i>10M</span>
          <span><i class="sw wl"></i>Wi-Fi</span>
          <span><ha-icon icon="mdi:home-assistant"></ha-icon>Known to HA</span>
          <span><ha-icon icon="mdi:flash"></ha-icon>PoE</span>
        </div>
      </div>
      <aside class="drawer" hidden></aside>
    `;
    const root = this.shadowRoot;
    root.querySelector(".site").addEventListener("change", (ev) => {
      this._siteId = ev.target.value;
      this._selected = null;
      this._fitted = false;
      this._render();
    });
    const search = root.querySelector(".search");
    search.addEventListener("input", () => {
      this._opts.query = search.value.trim().toLowerCase();
      this._applySearch();
    });
    search.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") this._focusFirstMatch();
      if (ev.key === "Escape") {
        search.value = "";
        this._opts.query = "";
        this._applySearch();
      }
    });
    root.querySelectorAll(".toggle").forEach((btn) =>
      btn.addEventListener("click", () => {
        const key = btn.dataset.opt;
        this._opts[key] = !this._opts[key];
        this._saveOpts();
        this._fitted = false;
        this._render();
      }),
    );
    root.querySelectorAll("[data-zoom]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const z = btn.dataset.zoom;
        if (z === "fit") this._fit();
        else this._zoomAt(z === "in" ? 1.25 : 0.8);
      }),
    );
    this._setupPanZoom(root.querySelector(".viewport"));
    root.querySelector(".stage").addEventListener("click", (ev) => {
      const node = ev.target.closest("[data-key]");
      if (node && !this._dragged) this._select(node.dataset.key);
    });
  }

  // ------------------------------------------------------------------ model

  _site() {
    return this._data?.sites.find((s) => s.id === this._siteId) || null;
  }

  _buildTree(site) {
    const devices = new Map(site.devices.map((d) => [d.id, { ...d, key: `d:${d.id}`, lanes: new Map() }]));
    const lane = (dev, key, make) => {
      if (!dev.lanes.has(key)) dev.lanes.set(key, { key, devices: [], clients: [], ...make() });
      return dev.lanes.get(key);
    };
    const portLane = (dev, number) => {
      const port = dev.ports.find((p) => p.number === number) || { number, label: number };
      return lane(dev, `p${number}`, () => ({ kind: "port", port, order: number }));
    };
    const roots = [];
    for (const dev of devices.values()) {
      const parent = devices.get(dev.uplink_id);
      if (parent && parent !== dev) {
        const number = dev.uplink_port ?? parent.ports.find((p) => p.device_id === dev.id && !p.uplink)?.number;
        if (number != null) portLane(parent, number).devices.push(dev);
        else lane(parent, "downstream", () => ({ kind: "other", label: "Downstream", order: 9999 })).devices.push(dev);
      } else {
        roots.push(dev);
      }
    }
    const o = this._opts;
    const stats = { online: 0, wireless: 0, wired: 0, shown: 0 };
    for (const c of site.clients) {
      if (c.online) {
        stats.online++;
        stats[c.wireless ? "wireless" : "wired"]++;
      }
      if (!c.online && !o.offline) continue;
      if (c.wireless ? !o.wireless : !o.wired) continue;
      const dev = devices.get(c.device_id) || roots[0];
      if (!dev) continue;
      stats.shown++;
      const item = { ...c, key: `c:${c.mac}` };
      if (c.wireless) {
        const radio = dev.radios.find((r) => r.band === c.band) || {};
        lane(dev, `b${c.band}`, () => ({ kind: "band", radio: { band: c.band, ...radio }, order: -10 + (c.band === "2.4ghz" ? 0 : c.band === "5ghz" ? 1 : 2) })).clients.push(item);
      } else if (c.port != null && devices.get(c.device_id)) {
        portLane(dev, c.port).clients.push(item);
      } else {
        lane(dev, "unknown", () => ({ kind: "other", label: "Unknown port", order: 10000 })).clients.push(item);
      }
    }
    for (const dev of devices.values()) {
      dev.laneList = [...dev.lanes.values()].sort((a, b) => a.order - b.order);
      for (const l of dev.laneList) {
        l.clients.sort((a, b) => b.online - a.online || String(a.name).localeCompare(String(b.name)));
        l.devices.sort((a, b) => String(a.name).localeCompare(String(b.name)));
      }
    }
    roots.sort((a, b) => (a.type === "switch" ? -1 : 1) - (b.type === "switch" ? -1 : 1) || String(a.name).localeCompare(String(b.name)));
    return { roots, devices, stats };
  }

  // ----------------------------------------------------------------- layout

  _faceplate(dev) {
    const n = dev.ports.length;
    if (!n) return { rows: 0, cols: 0, w: 0, h: 0 };
    const rows = n > 12 ? 2 : 1;
    const cols = Math.ceil(n / rows);
    return { rows, cols, w: cols * (PORT_W + PORT_GAP) - PORT_GAP, h: rows * (PORT_H + PORT_GAP) - PORT_GAP };
  }

  _cardSize(dev) {
    const fp = this._faceplate(dev);
    const w = Math.max(CARD_W, fp.w + 28);
    let h = 16 + 40 + 22; // padding, header, meta line
    if (fp.rows) h += fp.h + 12;
    if (dev.radios.length) h += 26;
    return { w, h, fp };
  }

  _measure(dev) {
    const card = this._cardSize(dev);
    dev.card = card;
    let lanesW = 0;
    let lanesH = 0;
    for (const l of dev.laneList) {
      let rowW = 0;
      let rowH = 0;
      for (const child of l.devices) {
        this._measure(child);
        rowW += (rowW ? GAP_X : 0) + child.size.w;
        rowH = Math.max(rowH, child.size.h);
      }
      const cols = Math.ceil(l.clients.length / CHIPS_PER_COL);
      const perCol = Math.min(l.clients.length, CHIPS_PER_COL);
      const chipsW = cols ? cols * CHIP_W + (cols - 1) * CHIP_GAP : 0;
      const chipsH = perCol ? perCol * (CHIP_H + CHIP_GAP) - CHIP_GAP : 0;
      l.cols = cols;
      l.w = Math.max(rowW, chipsW, 150);
      l.h = LANE_HEAD_H + LANE_HEAD_GAP + rowH + (rowH && chipsH ? GAP_Y / 2 : 0) + chipsH;
      l.rowH = rowH;
      l.rowW = rowW;
      lanesW += (lanesW ? GAP_X : 0) + l.w;
      lanesH = Math.max(lanesH, l.h);
    }
    dev.lanesW = lanesW;
    dev.size = { w: Math.max(card.w, lanesW), h: card.h + (dev.laneList.length ? GAP_Y + lanesH : 0) };
    return dev.size;
  }

  _place(dev, x, y, out) {
    const { w: cw, h: ch, fp } = dev.card;
    const cx = x + dev.size.w / 2;
    dev.pos = { x: cx - cw / 2, y, w: cw, h: ch };
    out.nodes.push({ type: "device", dev });
    const fpX = dev.pos.x + (cw - fp.w) / 2;
    const fpY = y + 16 + 40 + 22 + 4;
    const portPos = (number) => {
      const idx = dev.ports.findIndex((p) => p.number === number);
      if (idx < 0) return { x: cx, y: y + ch };
      const row = fp.rows === 2 ? idx % 2 : 0;
      const col = fp.rows === 2 ? Math.floor(idx / 2) : idx;
      return { x: fpX + col * (PORT_W + PORT_GAP) + PORT_W / 2, y: fpY + row * (PORT_H + PORT_GAP) + PORT_H / 2 };
    };
    let lx = cx - dev.lanesW / 2;
    const laneY = y + ch + GAP_Y;
    const bands = dev.laneList.filter((l) => l.kind === "band");
    for (const l of dev.laneList) {
      const hx = lx + l.w / 2;
      l.pos = { x: lx, y: laneY, w: l.w };
      out.lanes.push({ dev, lane: l });
      let sx = cx;
      let speed = null;
      let dashed = false;
      if (l.kind === "port") {
        sx = portPos(l.port.number).x;
        speed = l.port.speed;
      } else if (l.kind === "band") {
        const i = bands.indexOf(l);
        sx = dev.pos.x + cw * ((i + 1) / (bands.length + 1));
        dashed = true;
      }
      out.edges.push({ x1: sx, y1: y + ch, x2: hx, y2: laneY, cls: dashed ? "wl" : speedClass(speed), dashed });
      let rx = hx - l.rowW / 2;
      const rowY = laneY + LANE_HEAD_H + LANE_HEAD_GAP;
      for (const child of l.devices) {
        const childX = rx;
        this._place(child, childX, rowY, out);
        out.edges.push({
          x1: hx, y1: laneY + LANE_HEAD_H, x2: child.pos.x + child.pos.w / 2, y2: rowY,
          cls: speedClass(l.port?.speed ?? child.uplink_speed), dashed: false,
        });
        rx += child.size.w + GAP_X;
      }
      const chipsY = rowY + (l.rowH ? l.rowH + GAP_Y / 2 : 0);
      const chipsX = hx - (l.cols * CHIP_W + (l.cols - 1) * CHIP_GAP) / 2;
      l.clients.forEach((c, i) => {
        const col = Math.floor(i / CHIPS_PER_COL);
        const row = i % CHIPS_PER_COL;
        c.pos = { x: chipsX + col * (CHIP_W + CHIP_GAP), y: chipsY + row * (CHIP_H + CHIP_GAP) };
        out.nodes.push({ type: "client", client: c });
      });
      if (l.clients.length) {
        const guideX = chipsX - 10;
        out.guides.push({ x: guideX, y1: laneY + LANE_HEAD_H, y2: chipsY + Math.min(l.clients.length, CHIPS_PER_COL) * (CHIP_H + CHIP_GAP) - CHIP_GAP - CHIP_H / 2, hx, cls: l.kind === "band" ? "wl" : speedClass(l.port?.speed) });
      }
      lx += l.w + GAP_X;
    }
  }

  _layout(site, tree) {
    const out = { nodes: [], edges: [], lanes: [], guides: [], w: 0, h: 0, top: null };
    let x = PAD;
    let topY = PAD;
    const gw = site.gateways.length ? { w: 240, h: 52 } : null;
    let rootsW = 0;
    for (const r of tree.roots) {
      this._measure(r);
      rootsW += (rootsW ? GAP_X * 2 : 0) + r.size.w;
    }
    if (gw) topY += gw.h + GAP_Y;
    let maxH = 0;
    for (const r of tree.roots) {
      this._place(r, x, topY, out);
      x += r.size.w + GAP_X * 2;
      maxH = Math.max(maxH, r.size.h);
    }
    if (gw) {
      out.top = { x: PAD + rootsW / 2 - gw.w / 2, y: PAD, ...gw };
      for (const r of tree.roots) {
        const up = r.ports.find((p) => p.uplink);
        out.edges.push({
          x1: PAD + rootsW / 2, y1: PAD + gw.h, x2: r.pos.x + r.pos.w / 2, y2: r.pos.y,
          cls: speedClass(up?.speed), dashed: false, label: up ? `port ${up.label}${up.speed ? ` · ${speedLabel(up.speed)}` : ""}` : "",
        });
      }
    }
    out.w = Math.max(rootsW, gw?.w || 0) + PAD * 2;
    out.h = topY + maxH + PAD;
    return out;
  }

  // ----------------------------------------------------------------- render

  _render() {
    const root = this.shadowRoot;
    if (!root.querySelector(".stage")) return;
    const empty = root.querySelector(".empty");
    const siteSel = root.querySelector(".site");
    root.querySelectorAll(".toggle").forEach((b) => b.classList.toggle("on", !!this._opts[b.dataset.opt]));

    if (this._error) {
      empty.hidden = false;
      empty.textContent = `Could not load the network map: ${this._error}`;
      return;
    }
    const site = this._site();
    if (!site) {
      empty.hidden = false;
      empty.textContent = this._data ? "No Instant On sites are loaded." : "Waiting for Instant On data…";
      root.querySelector(".stage").innerHTML = "";
      return;
    }
    empty.hidden = true;

    const sites = this._data.sites;
    siteSel.hidden = sites.length < 2;
    siteSel.innerHTML = sites.map((s) => `<option value="${esc(s.id)}" ${s.id === site.id ? "selected" : ""}>${esc(s.name)}</option>`).join("");

    const tree = this._buildTree(site);
    const layout = this._layout(site, tree);
    this._layoutSize = { w: layout.w, h: layout.h };
    this._nodesByKey = new Map();

    const up = site.devices.filter((d) => d.status === "up").length;
    const alert = site.latest_alert;
    root.querySelector(".statusbar").innerHTML = `
      <span class="badge h-${esc(site.health)}"><ha-icon icon="mdi:heart-pulse"></ha-icon>${esc(site.health || "unknown")}${site.health_score != null ? ` · ${site.health_score}%` : ""}</span>
      <span class="badge"><ha-icon icon="mdi:router-network"></ha-icon>${up}/${site.devices.length} devices up</span>
      <span class="badge"><ha-icon icon="mdi:devices"></ha-icon>${tree.stats.online} clients online (${tree.stats.wireless} Wi-Fi · ${tree.stats.wired} wired)</span>
      ${site.active_alerts ? `<span class="badge h-warning" title="${esc(alert?.type || "")}"><ha-icon icon="mdi:alert-circle-outline"></ha-icon>${site.active_alerts} alert${site.active_alerts > 1 ? "s" : ""}${alert?.subject ? ` · ${esc(alert.subject)}` : ""}</span>` : ""}
      <span class="updated">${this._data.updated ? `Updated ${new Date(this._data.updated).toLocaleTimeString()}` : ""}</span>
    `;

    const svg = [];
    const curve = (e) => {
      const my = (e.y1 + e.y2) / 2;
      return `M${e.x1},${e.y1} C${e.x1},${my} ${e.x2},${my} ${e.x2},${e.y2}`;
    };
    for (const e of layout.edges) {
      svg.push(`<path class="edge ${e.cls}${e.dashed ? " dashed" : ""}" d="${curve(e)}"/>`);
      if (e.label) {
        const lx = (e.x1 + e.x2) / 2;
        const ly = (e.y1 + e.y2) / 2;
        svg.push(`<text class="elabel" x="${lx}" y="${ly}" text-anchor="middle" dominant-baseline="middle">${esc(e.label)}</text>`);
      }
    }
    for (const g of layout.guides) {
      svg.push(`<path class="guide ${g.cls}${g.cls === "wl" ? " dashed" : ""}" d="M${g.hx},${g.y1} L${g.hx},${g.y1 + 6} Q${g.hx},${g.y1 + 12} ${g.hx - 6},${g.y1 + 12} L${g.x + 6},${g.y1 + 12} Q${g.x},${g.y1 + 12} ${g.x},${g.y1 + 18} L${g.x},${g.y2}"/>`);
    }

    const html = [];
    if (layout.top) {
      const t = layout.top;
      html.push(`<div class="gateway" style="left:${t.x}px;top:${t.y}px;width:${t.w}px;height:${t.h}px">
        <ha-icon icon="mdi:router-network"></ha-icon><div><div class="name">Gateway</div><div class="sub">${esc(site.gateways.join(", "))}</div></div></div>`);
    }
    for (const { lane: l } of layout.lanes) {
      html.push(this._laneHtml(l));
    }
    for (const n of layout.nodes) {
      if (n.type === "device") {
        this._nodesByKey.set(n.dev.key, n.dev);
        html.push(this._deviceHtml(n.dev));
      } else {
        this._nodesByKey.set(n.client.key, n.client);
        html.push(this._clientHtml(n.client));
      }
    }

    const stage = root.querySelector(".stage");
    stage.style.width = `${layout.w}px`;
    stage.style.height = `${layout.h}px`;
    stage.innerHTML = `<svg class="edges" width="${layout.w}" height="${layout.h}">${svg.join("")}</svg>${html.join("")}`;

    if (!this._fitted) this._fit();
    else this._applyView();
    this._applySearch();
    if (this._selected) this._select(this._selected, true);
  }

  _laneHtml(l) {
    let icon;
    let label;
    let extra = "";
    let cls = "";
    if (l.kind === "port") {
      const p = l.port;
      icon = p.sfp ? "mdi:expansion-card-variant" : "mdi:ethernet";
      label = `Port ${esc(p.label)}`;
      if (p.speed) extra += `<span class="pill ${speedClass(p.speed)}">${speedLabel(p.speed)}</span>`;
      if (p.poe_watts) extra += `<span class="pill poe"><ha-icon icon="mdi:flash"></ha-icon>${p.poe_watts} W</span>`;
      if (p.problems?.length) {
        extra += `<span class="pill bad" title="${esc(p.problems.join(", "))}"><ha-icon icon="mdi:alert"></ha-icon></span>`;
        cls = " problem";
      }
    } else if (l.kind === "band") {
      const r = l.radio;
      icon = "mdi:access-point";
      label = bandLabel(r.band);
      if (r.channel != null) extra += `<span class="pill">ch ${esc(r.channel)}</span>`;
      if (r.utilization != null) extra += `<span class="pill ${r.utilization >= 60 ? "bad" : r.utilization >= 35 ? "warn" : ""}">${r.utilization}%</span>`;
    } else {
      icon = "mdi:help-network-outline";
      label = esc(l.label);
    }
    const count = l.clients.length ? `<span class="count">${l.clients.length}</span>` : "";
    return `<div class="lane${cls}" style="left:${l.pos.x + l.pos.w / 2}px;top:${l.pos.y}px">
      <ha-icon icon="${icon}"></ha-icon><span class="lname">${label}</span>${extra}${count}</div>`;
  }

  _deviceHtml(d) {
    const { x, y, w, h } = d.pos;
    const { fp } = d.card;
    const state = d.status !== "up" ? "down" : d.health === "good" ? "good" : "warn";
    const icon = d.type === "accessPoint" ? "mdi:access-point" : d.type === "switch" ? "mdi:switch" : "mdi:router-network";
    const wireless = d.radios.reduce((s, r) => s + (r.clients || 0), 0);
    const meta = [
      d.area ? `<span class="area"><ha-icon icon="mdi:texture-box"></ha-icon>${esc(d.area)}</span>` : "",
      `<span title="Uptime"><ha-icon icon="mdi:timer-outline"></ha-icon>${duration(d.uptime)}</span>`,
      d.radios.length ? `<span title="Wireless clients"><ha-icon icon="mdi:wifi"></ha-icon>${wireless}</span>` : "",
      d.poe_budget != null ? `<span title="PoE used / budget"><ha-icon icon="mdi:flash"></ha-icon>${d.poe_used ?? 0}/${d.poe_budget} W</span>` : "",
      d.up_to_date === false ? `<span class="warn" title="Firmware update pending"><ha-icon icon="mdi:update"></ha-icon></span>` : "",
    ].join("");
    let ports = "";
    if (fp.rows) {
      ports = `<div class="faceplate" style="grid-template-columns:repeat(${fp.cols},${PORT_W}px);grid-template-rows:repeat(${fp.rows},${PORT_H}px)">`;
      d.ports.forEach((p, idx) => {
        const row = fp.rows === 2 ? (idx % 2) + 1 : 1;
        const col = fp.rows === 2 ? Math.floor(idx / 2) + 1 : idx + 1;
        const cls = [
          "port",
          p.up ? speedClass(p.speed) : p.disabled ? "disabled" : "off",
          p.uplink ? "uplink" : "",
          p.poe_watts ? "powered" : "",
          p.problems?.length ? "problem" : "",
          p.sfp ? "sfp" : "",
        ].join(" ");
        const tip = `Port ${p.label}${p.name ? ` (${p.name})` : ""}: ${p.up ? `up ${speedLabel(p.speed)}` : p.disabled ? "disabled" : "down"}${p.uplink ? " · uplink" : ""}${p.poe_watts ? ` · PoE ${p.poe_watts} W` : ""}${p.problems?.length ? ` · ${p.problems.join(", ")}` : ""}`;
        ports += `<div class="${cls}" style="grid-row:${row};grid-column:${col}" title="${esc(tip)}">${esc(p.label)}</div>`;
      });
      ports += `</div>`;
    }
    const radios = d.radios.length
      ? `<div class="radios">${d.radios
          .map((r) => `<span class="${r.utilization >= 60 ? "bad" : r.utilization >= 35 ? "warn" : ""}" title="${esc(bandLabel(r.band))}: channel ${esc(r.channel)} ${esc(r.width || "")}, ${r.utilization ?? "?"}% utilized, ${r.clients ?? 0} clients">${esc(bandLabel(r.band))} · ch ${esc(r.channel)} · ${r.utilization ?? "?"}%</span>`)
          .join("")}</div>`
      : "";
    return `<div class="node device ${state}" data-key="${esc(d.key)}" data-search="${esc(`${d.name} ${d.ha_name || ""} ${d.mac} ${d.ip} ${d.model} ${d.area || ""}`.toLowerCase())}"
        style="left:${x}px;top:${y}px;width:${w}px;height:${h}px">
      <div class="dhead"><div class="dicon"><ha-icon icon="${icon}"></ha-icon></div>
        <div class="dtitle"><div class="name">${esc(d.ha_name || d.name)}</div><div class="sub">${esc(d.model)} · ${esc(d.ip || "no IP")}</div></div>
        <span class="state ${state}" title="${esc(d.status)} / ${esc(d.health)}"></span></div>
      <div class="meta">${meta}</div>${ports}${radios}</div>`;
  }

  _clientHtml(c) {
    const icon = c.wireless ? qualityIcon(c.quality) : "mdi:ethernet";
    const sub = [c.ip, c.wireless && c.signal != null ? `${c.signal} dBm` : null, !c.wireless && c.port_speed ? speedLabel(c.port_speed) : null]
      .filter(Boolean)
      .join(" · ");
    const badges = [
      c.ha_device_id && c.ha_device_name ? `<ha-icon class="ha" icon="mdi:home-assistant" title="${esc(c.ha_device_name)}"></ha-icon>` : "",
      c.watchlisted ? `<ha-icon icon="mdi:eye" title="Watchlisted"></ha-icon>` : "",
      c.blocked ? `<ha-icon class="bad" icon="mdi:cancel" title="Blocked"></ha-icon>` : "",
    ].join("");
    const cls = ["node", "client", c.online ? "online" : "offline", c.wireless ? `q-${c.quality || "none"}` : "wired", c.health === "poor" ? "poor" : ""].join(" ");
    return `<div class="${cls}" data-key="${esc(c.key)}" data-search="${esc(`${c.name} ${c.ha_device_name || ""} ${c.mac} ${c.ip || ""} ${c.network || ""} ${c.area || ""}`.toLowerCase())}"
        style="left:${c.pos.x}px;top:${c.pos.y}px;width:${CHIP_W}px;height:${CHIP_H}px"
        title="${esc(c.name)}\n${esc(c.mac)}">
      <ha-icon class="cicon" icon="${icon}"></ha-icon>
      <div class="ctext"><div class="name">${esc(c.name)}</div><div class="sub">${esc(sub || c.mac)}</div></div>
      <div class="badges">${badges}</div></div>`;
  }

  // ---------------------------------------------------------------- details

  _select(key, keepOpen = false) {
    const drawer = this.shadowRoot.querySelector(".drawer");
    const item = this._nodesByKey?.get(key);
    this.shadowRoot.querySelectorAll(".node.selected").forEach((n) => n.classList.remove("selected"));
    if (!item || (!keepOpen && this._selected === key && !drawer.hidden)) {
      this._selected = null;
      drawer.hidden = true;
      return;
    }
    this._selected = key;
    this.shadowRoot.querySelector(`[data-key="${CSS.escape(key)}"]`)?.classList.add("selected");
    drawer.hidden = false;
    drawer.innerHTML = key.startsWith("d:") ? this._deviceDetails(item) : this._clientDetails(item);
    drawer.querySelector(".close").addEventListener("click", () => this._select(key));
    drawer.querySelectorAll("[data-nav]").forEach((b) =>
      b.addEventListener("click", () => {
        history.pushState(null, "", b.dataset.nav);
        window.dispatchEvent(new CustomEvent("location-changed"));
      }),
    );
    drawer.querySelectorAll("[data-entity]").forEach((b) =>
      b.addEventListener("click", () =>
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: b.dataset.entity }, bubbles: true, composed: true })),
      ),
    );
    drawer.querySelectorAll("[data-copy]").forEach((b) =>
      b.addEventListener("click", () => navigator.clipboard?.writeText(b.dataset.copy)),
    );
  }

  _rows(rows) {
    return `<table>${rows
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`)
      .join("")}</table>`;
  }

  _deviceDetails(d) {
    const portsUp = d.ports.filter((p) => p.up).length;
    const ports = d.ports.length
      ? `<h3>Ports (${portsUp}/${d.ports.length} up)</h3><table class="ports"><tr><th>#</th><th>Link</th><th>PoE</th><th>Traffic ↓/↑</th></tr>${d.ports
          .map((p) => `<tr class="${p.up ? "" : "dim"}"><td>${esc(p.label)}${p.uplink ? " ⬆" : ""}</td><td>${p.up ? `<span class="pill ${speedClass(p.speed)}">${speedLabel(p.speed)}</span>` : p.disabled ? "disabled" : "down"}${p.problems?.length ? ` <span class="pill bad">${esc(p.problems.join(", "))}</span>` : ""}</td><td>${p.poe ? (p.poe_watts ? `${p.poe_watts} W` : "–") : ""}</td><td>${p.up ? `${bps(p.down_bps)} / ${bps(p.up_bps)}` : ""}</td></tr>`)
          .join("")}</table>`
      : "";
    const radios = d.radios.length
      ? `<h3>Radios</h3>${this._rows(d.radios.map((r) => [bandLabel(r.band), `ch ${esc(r.channel)} (${esc(r.width || "?")}) · ${r.utilization ?? "?"}% · ${r.clients ?? 0} clients · ${r.tx_power ?? "?"} dBm`]))}`
      : "";
    return `<header><ha-icon icon="${d.type === "accessPoint" ? "mdi:access-point" : "mdi:switch"}"></ha-icon><h2>${esc(d.ha_name || d.name)}</h2><button class="close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button></header>
      <div class="actions">
        ${d.ha_device_id ? `<button data-nav="/config/devices/device/${esc(d.ha_device_id)}"><ha-icon icon="mdi:devices"></ha-icon>Device page</button>` : ""}
        <button data-copy="${esc(d.mac)}"><ha-icon icon="mdi:content-copy"></ha-icon>Copy MAC</button>
      </div>
      ${this._rows([
        ["Status", `${esc(d.status)} · health ${esc(d.health)}`],
        ["Area", d.area ? esc(d.area) : `<span class="dim">not assigned</span>`],
        ["Model", esc(d.model)],
        ["IP", esc(d.ip)],
        ["MAC", esc(d.mac)],
        ["Firmware", `${esc(d.firmware)}${d.up_to_date === false ? " (update pending)" : ""}`],
        ["Uptime", duration(d.uptime)],
        ["Uplink", d.uplink_id ? `${esc(this._nodesByKey.get(`d:${d.uplink_id}`)?.name || d.uplink_id)}${d.uplink_port != null ? ` · port ${esc(d.uplink_port)}` : ""}` : "gateway"],
        ["Power", d.poe_budget != null ? `${d.poe_used ?? 0} / ${d.poe_budget} W PoE` : esc(d.power_source)],
      ])}${radios}${ports}`;
  }

  _clientDetails(c) {
    const dev = this._nodesByKey.get(`d:${c.device_id}`);
    const where = c.wireless
      ? `${esc(dev?.name || c.device_id)} · ${esc(bandLabel(c.band))}`
      : `${esc(dev?.name || c.device_id)}${c.port != null ? ` · port ${esc(c.port)}` : ""}${c.port_speed ? ` · ${speedLabel(c.port_speed)}` : ""}`;
    return `<header><ha-icon icon="${c.wireless ? qualityIcon(c.quality) : "mdi:ethernet"}"></ha-icon><h2>${esc(c.name)}</h2><button class="close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button></header>
      <div class="actions">
        ${c.ha_device_id ? `<button data-nav="/config/devices/device/${esc(c.ha_device_id)}"><ha-icon icon="mdi:home-assistant"></ha-icon>${esc(c.ha_device_name || "Device")}</button>` : ""}
        ${c.tracker_entity_id ? `<button data-entity="${esc(c.tracker_entity_id)}"><ha-icon icon="mdi:crosshairs-gps"></ha-icon>Tracker</button>` : ""}
        <button data-copy="${esc(c.mac)}"><ha-icon icon="mdi:content-copy"></ha-icon>Copy MAC</button>
      </div>
      ${this._rows([
        ["Status", c.online ? `online · ${duration(c.connected_for)}` : `offline since ${since(c.since)}`],
        ["Connected to", where],
        ["Area", c.area ? esc(c.area) : null],
        ["IP", esc(c.ip)],
        ["MAC", esc(c.mac)],
        ["Network", `${esc(c.network)}${c.vlan != null ? ` · VLAN ${esc(c.vlan)}` : ""}`],
        ["Signal", c.wireless && c.signal != null ? `${c.signal} dBm · SNR ${esc(c.snr)} dB · ${esc(c.quality)}` : null],
        ["Health", c.health_pct != null ? `${c.health_pct}%` : esc(c.health)],
        ["Traffic (24h)", `↓ ${bytes(c.down_24h)} · ↑ ${bytes(c.up_24h)}`],
        ["OS", c.os && c.os !== "unknown" ? esc(c.os) : null],
        ["Watchlisted", c.watchlisted ? "yes" : null],
        ["Tracked in HA", c.tracker_entity_id ? esc(c.tracker_entity_id) : "no"],
      ])}`;
  }

  // ----------------------------------------------------------- search, view

  _applySearch() {
    const q = this._opts.query;
    const nodes = this.shadowRoot.querySelectorAll(".node");
    nodes.forEach((n) => {
      const hit = q && n.dataset.search.includes(q);
      n.classList.toggle("match", !!hit);
      n.classList.toggle("dim", !!q && !hit);
    });
  }

  _focusFirstMatch() {
    const node = this.shadowRoot.querySelector(".node.match");
    if (!node) return;
    const vp = this.shadowRoot.querySelector(".viewport").getBoundingClientRect();
    // The details drawer opens on the right; center in the space left of it.
    const visibleW = this._narrow ? vp.width : vp.width - Math.min(380, vp.width);
    const x = parseFloat(node.style.left) + parseFloat(node.style.width) / 2;
    const y = parseFloat(node.style.top) + parseFloat(node.style.height) / 2;
    this._view.k = Math.max(this._view.k, 1);
    this._view.x = visibleW / 2 - x * this._view.k;
    this._view.y = vp.height / 2 - y * this._view.k;
    this._applyView(true);
    this._select(node.dataset.key, true);
  }

  _fit() {
    const vp = this.shadowRoot.querySelector(".viewport");
    if (!vp || !this._layoutSize) return;
    const { width, height } = vp.getBoundingClientRect();
    if (!width || !height) return;
    const k = Math.min(width / this._layoutSize.w, height / this._layoutSize.h, 1);
    this._view = { k, x: (width - this._layoutSize.w * k) / 2, y: Math.max(0, (height - this._layoutSize.h * k) / 2) };
    this._fitted = true;
    this._applyView(true);
  }

  _zoomAt(factor, cx, cy) {
    const vp = this.shadowRoot.querySelector(".viewport").getBoundingClientRect();
    cx ??= vp.width / 2;
    cy ??= vp.height / 2;
    const k = Math.min(3, Math.max(0.1, this._view.k * factor));
    this._view.x = cx - ((cx - this._view.x) * k) / this._view.k;
    this._view.y = cy - ((cy - this._view.y) * k) / this._view.k;
    this._view.k = k;
    this._fitted = true;
    this._applyView();
  }

  _applyView(animate = false) {
    const stage = this.shadowRoot.querySelector(".stage");
    stage.classList.toggle("animate", animate);
    stage.style.transform = `translate(${this._view.x}px, ${this._view.y}px) scale(${this._view.k})`;
  }

  _setupPanZoom(vp) {
    vp.addEventListener(
      "wheel",
      (ev) => {
        ev.preventDefault();
        const r = vp.getBoundingClientRect();
        this._zoomAt(Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)), ev.clientX - r.left, ev.clientY - r.top);
      },
      { passive: false },
    );
    let last = null;
    let pinch = null;
    vp.addEventListener("pointerdown", (ev) => {
      if (ev.target.closest(".legend")) return;
      this._pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      this._dragged = false;
      last = { x: ev.clientX, y: ev.clientY };
      if (this._pointers.size === 2) {
        const [a, b] = [...this._pointers.values()];
        pinch = Math.hypot(a.x - b.x, a.y - b.y);
      }
    });
    vp.addEventListener("pointermove", (ev) => {
      if (!this._pointers.has(ev.pointerId)) return;
      this._pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (this._pointers.size === 2 && pinch) {
        const [a, b] = [...this._pointers.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        const r = vp.getBoundingClientRect();
        this._zoomAt(dist / pinch, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
        pinch = dist;
        this._dragged = true;
        return;
      }
      const dx = ev.clientX - last.x;
      const dy = ev.clientY - last.y;
      if (!this._dragged && Math.hypot(dx, dy) < 4) return;
      if (!this._dragged) vp.setPointerCapture(ev.pointerId);
      this._dragged = true;
      vp.classList.add("dragging");
      this._view.x += dx;
      this._view.y += dy;
      last = { x: ev.clientX, y: ev.clientY };
      this._fitted = true;
      this._applyView();
    });
    const end = (ev) => {
      this._pointers.delete(ev.pointerId);
      if (this._pointers.size < 2) pinch = null;
      vp.classList.remove("dragging");
      setTimeout(() => (this._dragged = false), 0);
    };
    vp.addEventListener("pointerup", end);
    vp.addEventListener("pointercancel", end);
  }
}

const STYLES = `
  :host {
    display: flex; flex-direction: column; height: 100vh; height: 100dvh;
    background: var(--primary-background-color, #fafafa);
    color: var(--primary-text-color, #212121);
    font-family: var(--paper-font-body1_-_font-family, Roboto, "Noto Sans", sans-serif);
    --mdc-icon-size: 18px;
    --ok: var(--success-color, #43a047);
    --warn: var(--warning-color, #ffa000);
    --bad: var(--error-color, #db4437);
    --card: var(--card-background-color, #fff);
    --line: var(--divider-color, rgba(0,0,0,.12));
    --muted: var(--secondary-text-color, #727272);
    --accent: var(--primary-color, #03a9f4);
    --s-multi: #8e5cf5;
    --s-gig: var(--ok);
    --s-fast: var(--warn);
    --s-slow: var(--bad);
    --wl: var(--accent);
  }
  ha-icon { display: inline-flex; }
  button { font: inherit; color: inherit; }
  .toolbar {
    display: flex; align-items: center; gap: 12px; height: 56px; padding: 0 12px;
    background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, var(--text-primary-color, #fff));
    border-bottom: var(--app-header-border-bottom, none);
    --mdc-icon-size: 24px;
  }
  .toolbar .title { font-size: 20px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  :host(:not([narrow])) ha-menu-button { display: none; }
  .spacer { flex: 1; }
  .site, .search {
    height: 36px; border-radius: 18px; border: none; padding: 0 14px; font: inherit;
    background: color-mix(in srgb, currentColor 10%, transparent); color: inherit; outline: none;
  }
  .site option { color: #000; }
  .search { width: min(280px, 40vw); }
  .search::placeholder { color: inherit; opacity: .75; }
  .statusbar, .controls {
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 12px;
    background: var(--card); border-bottom: 1px solid var(--line);
  }
  .controls { padding-top: 6px; padding-bottom: 6px; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 14px;
    background: var(--secondary-background-color, #f0f0f0); font-size: 13px;
  }
  .badge.h-good { color: var(--ok); }
  .badge.h-warning, .badge.h-fair { color: var(--warn); }
  .badge.h-poor, .badge.h-critical { color: var(--bad); }
  .updated { margin-left: auto; color: var(--muted); font-size: 12px; }
  .toggle, .icon {
    display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line);
    background: transparent; border-radius: 16px; padding: 4px 12px; cursor: pointer; font-size: 13px;
  }
  .toggle.on { background: color-mix(in srgb, var(--accent) 16%, transparent); border-color: var(--accent); color: var(--accent); }
  .toggle:not(.on) { color: var(--muted); }
  .icon { padding: 4px 8px; }
  .viewport { position: relative; flex: 1; overflow: hidden; cursor: grab; touch-action: none; }
  .viewport.dragging { cursor: grabbing; }
  .stage { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
  .stage.animate { transition: transform .25s ease; }
  .edges { position: absolute; left: 0; top: 0; overflow: visible; pointer-events: none; }
  .edge, .guide { fill: none; stroke-width: 2.5; stroke-linecap: round; }
  .guide { stroke-width: 2; opacity: .55; }
  .dashed { stroke-dasharray: 6 6; }
  .s-multi { stroke: var(--s-multi); } .s-gig { stroke: var(--s-gig); } .s-fast { stroke: var(--s-fast); }
  .s-slow { stroke: var(--s-slow); } .s-none { stroke: var(--line); } .wl { stroke: var(--wl); }
  .elabel { font-size: 12px; fill: var(--muted); paint-order: stroke; stroke: var(--primary-background-color, #fafafa); stroke-width: 5px; }
  .empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--muted); }
  .empty[hidden] { display: none; }

  .gateway {
    position: absolute; box-sizing: border-box; display: flex; align-items: center; gap: 12px; padding: 0 18px;
    border-radius: 26px; background: var(--card); border: 2px solid var(--accent);
    box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,.12)); --mdc-icon-size: 26px; color: var(--accent);
  }
  .gateway .name { font-weight: 500; color: var(--primary-text-color); }
  .gateway .sub { font-size: 12px; color: var(--muted); }

  .lane {
    position: absolute; transform: translateX(-50%); height: ${LANE_HEAD_H}px; box-sizing: border-box;
    display: inline-flex; align-items: center; gap: 6px; padding: 0 10px 0 8px; white-space: nowrap;
    background: var(--card); border: 1px solid var(--line); border-radius: 14px; font-size: 12px;
    --mdc-icon-size: 16px; color: var(--muted); z-index: 1;
  }
  .lane .lname { color: var(--primary-text-color); font-weight: 500; }
  .lane.problem { border-color: var(--bad); }
  .pill {
    display: inline-flex; align-items: center; gap: 2px; padding: 1px 7px; border-radius: 9px; font-size: 11px;
    background: var(--secondary-background-color, #eee); color: var(--primary-text-color); --mdc-icon-size: 12px;
  }
  .pill.s-multi { background: color-mix(in srgb, var(--s-multi) 22%, transparent); }
  .pill.s-gig { background: color-mix(in srgb, var(--s-gig) 22%, transparent); }
  .pill.s-fast { background: color-mix(in srgb, var(--s-fast) 25%, transparent); }
  .pill.s-slow, .pill.bad { background: color-mix(in srgb, var(--bad) 25%, transparent); }
  .pill.warn { background: color-mix(in srgb, var(--warn) 25%, transparent); }
  .pill.poe { background: color-mix(in srgb, #fdd835 35%, transparent); }
  .count {
    min-width: 18px; height: 18px; border-radius: 9px; padding: 0 5px; box-sizing: border-box;
    display: inline-flex; align-items: center; justify-content: center;
    background: var(--accent); color: var(--text-primary-color, #fff); font-size: 11px; font-weight: 500;
  }

  .node { position: absolute; box-sizing: border-box; cursor: pointer; transition: opacity .15s, box-shadow .15s; z-index: 2; }
  .node.dim { opacity: .25; }
  .node.match { box-shadow: 0 0 0 3px var(--accent) !important; }
  .node.selected { box-shadow: 0 0 0 3px var(--accent), 0 6px 18px rgba(0,0,0,.2) !important; }

  .device {
    padding: 8px 12px; border-radius: var(--ha-card-border-radius, 12px); background: var(--card);
    border: 1px solid var(--line); box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,.1));
    border-top: 4px solid var(--ok);
  }
  .device.warn { border-top-color: var(--warn); }
  .device.down { border-top-color: var(--bad); background: color-mix(in srgb, var(--bad) 8%, var(--card)); }
  .dhead { display: flex; align-items: center; gap: 10px; height: 40px; }
  .dicon {
    width: 36px; height: 36px; border-radius: 50%; flex: none; display: flex; align-items: center; justify-content: center;
    background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent); --mdc-icon-size: 22px;
  }
  .dtitle { flex: 1; min-width: 0; }
  .name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .state { width: 10px; height: 10px; border-radius: 50%; flex: none; background: var(--ok); }
  .state.warn { background: var(--warn); } .state.down { background: var(--bad); }
  .meta {
    display: flex; gap: 10px; height: 22px; align-items: center; font-size: 12px; color: var(--muted);
    --mdc-icon-size: 14px; white-space: nowrap; overflow: hidden;
  }
  .meta span { display: inline-flex; align-items: center; gap: 3px; }
  .meta .area { color: var(--accent); }
  .meta .warn { color: var(--warn); }
  .faceplate {
    display: grid; gap: ${PORT_GAP}px; justify-content: center; margin: 4px 0 8px;
    padding: 0; font-size: 8px; line-height: ${PORT_H}px;
  }
  .port {
    border-radius: 3px; text-align: center; color: #fff; font-weight: 600; overflow: hidden;
    background: var(--s-none, #bbb); position: relative;
  }
  .port.off { background: color-mix(in srgb, var(--muted) 25%, transparent); color: var(--muted); }
  .port.disabled { background: repeating-linear-gradient(45deg, transparent 0 3px, color-mix(in srgb, var(--muted) 35%, transparent) 3px 5px); color: var(--muted); }
  .port.s-multi { background: var(--s-multi); } .port.s-gig { background: var(--s-gig); }
  .port.s-fast { background: var(--s-fast); } .port.s-slow { background: var(--s-slow); }
  .port.uplink { outline: 2px solid var(--accent); outline-offset: 1px; }
  .port.powered::after {
    content: ""; position: absolute; right: 1px; top: 1px; width: 4px; height: 4px; border-radius: 50%; background: #fdd835;
  }
  .port.problem { animation: blink 1s infinite; }
  .port.sfp { border-radius: 1px; }
  @keyframes blink { 50% { background: var(--bad); } }
  .radios { display: flex; gap: 6px; height: 22px; align-items: center; font-size: 11px; }
  .radios span { padding: 2px 8px; border-radius: 10px; background: color-mix(in srgb, var(--wl) 14%, transparent); white-space: nowrap; }
  .radios span.warn { background: color-mix(in srgb, var(--warn) 25%, transparent); }
  .radios span.bad { background: color-mix(in srgb, var(--bad) 25%, transparent); }

  .client {
    display: flex; align-items: center; gap: 8px; padding: 0 8px 0 10px; border-radius: 21px;
    background: var(--card); border: 1px solid var(--line); box-shadow: 0 1px 2px rgba(0,0,0,.06);
  }
  .client .cicon { color: var(--accent); --mdc-icon-size: 20px; flex: none; }
  .client.wired .cicon { color: var(--s-gig); }
  .client.q-fair .cicon { color: var(--warn); }
  .client.q-poor .cicon, .client.poor .cicon { color: var(--bad); }
  .client.offline { opacity: .55; border-style: dashed; }
  .client.offline .cicon { color: var(--muted); }
  .ctext { flex: 1; min-width: 0; line-height: 1.2; }
  .ctext .name { font-size: 13px; }
  .ctext .sub { font-size: 11px; }
  .badges { display: flex; gap: 2px; --mdc-icon-size: 15px; color: var(--muted); }
  .badges .ha { color: #18bcf2; }
  .badges .bad { color: var(--bad); }

  .legend {
    position: absolute; left: 12px; bottom: 12px; display: flex; flex-wrap: wrap; gap: 12px; max-width: calc(100% - 24px);
    padding: 6px 12px; border-radius: 14px; background: color-mix(in srgb, var(--card) 90%, transparent);
    border: 1px solid var(--line); font-size: 12px; color: var(--muted); --mdc-icon-size: 14px; cursor: default;
  }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .legend .sw { display: inline-block; width: 18px; height: 0; border-top: 3px solid; }
  .legend .sw.s-multi { border-color: var(--s-multi); } .legend .sw.s-gig { border-color: var(--s-gig); }
  .legend .sw.s-fast { border-color: var(--s-fast); } .legend .sw.s-slow { border-color: var(--s-slow); }
  .legend .sw.wl { border-top: 3px dashed var(--wl); }
  .legend ha-icon[icon="mdi:home-assistant"] { color: #18bcf2; }
  .legend ha-icon[icon="mdi:flash"] { color: #f9a825; }

  .drawer {
    position: absolute; right: 0; top: 56px; bottom: 0; width: min(380px, 100%); overflow: auto; z-index: 5;
    background: var(--card); border-left: 1px solid var(--line); box-shadow: -4px 0 16px rgba(0,0,0,.12);
    padding: 0 16px 16px; box-sizing: border-box;
  }
  :host { position: relative; }
  .drawer[hidden] { display: none; }
  .drawer header { display: flex; align-items: center; gap: 10px; position: sticky; top: 0; background: var(--card); padding: 14px 0 8px; --mdc-icon-size: 24px; color: var(--accent); }
  .drawer h2 { flex: 1; margin: 0; font-size: 18px; font-weight: 500; color: var(--primary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .drawer h3 { margin: 16px 0 6px; font-size: 14px; font-weight: 500; }
  .drawer .close { border: none; background: none; cursor: pointer; color: var(--muted); }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 12px; }
  .actions button {
    display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 16px;
    background: transparent; padding: 4px 12px; cursor: pointer; font-size: 13px; color: var(--accent);
  }
  .drawer table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .drawer th { text-align: left; font-weight: 400; color: var(--muted); padding: 5px 8px 5px 0; vertical-align: top; white-space: nowrap; }
  .drawer td { padding: 5px 0; word-break: break-word; }
  .drawer table.ports th, .drawer table.ports td { padding: 3px 6px 3px 0; }
  .drawer tr.dim, .dim { color: var(--muted); }
  :host([narrow]) .legend { display: none; }
  :host([narrow]) .search { width: 40vw; }
`;

if (!customElements.get("instant-on-panel")) customElements.define("instant-on-panel", InstantOnPanel);
