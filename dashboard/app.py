#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mihomo web dashboard: multi-subscription management, switch, traffic & usage stats."""
import json, os, time, sqlite3, threading, urllib.request, urllib.error, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta

MIHOMO = "http://mihomo:9090"
SECRET = os.environ.get("MIHOMO_SECRET", "change-me-please")
INIT_SUB_URL = os.environ.get("INIT_SUB_URL", "").strip()
DB_PATH = "/data/usage.db"
CONFIG_PATH = "/mihomo/config.yaml"
MIHOMO_CONFIG_PATH = "/root/.config/mihomo/config.yaml"
HEADERS = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
db_lock = threading.Lock()

# in-memory caches shared between worker and request handlers
traffic_cache = {"up": 0, "down": 0, "upTotal": 0, "downTotal": 0, "ts": 0}
delay_cache = {}          # node name -> delay in ms
delay_lock = threading.Lock()
TEST_URL = "http://www.gstatic.com/generate_204"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS samples (
        ts INTEGER PRIMARY KEY, up INTEGER, down INTEGER,
        uptotal INTEGER, downtotal INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS active (
        day TEXT PRIMARY KEY, seconds INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, detail TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, url TEXT NOT NULL,
        enabled INTEGER DEFAULT 1, created_at INTEGER)""")
    conn.commit()
    conn.close()


def get_subs():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id,name,url,enabled,created_at FROM subs ORDER BY id")
    rows = [{"id": r[0], "name": r[1], "url": r[2],
             "enabled": bool(r[3]), "created_at": r[4]} for r in c.fetchall()]
    conn.close()
    return rows


def read_current_sub_url():
    """Migrate the hard-coded subscription from the first-generation config.yaml."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            txt = f.read()
        block = txt.split("proxy-providers:", 1)[1].split("proxy-groups:", 1)[0]
        m = re.search(r"url:\s*'([^']+)'", block)
        return m.group(1) if m else None
    except Exception:
        return None


def migrate_subs():
    """Seed the first subscription from .env, or migrate an old config.yaml.

    Placeholder URLs are ignored so a fresh clone never tries to fetch a fake sub.
    """
    if get_subs():
        return
    url = INIT_SUB_URL or read_current_sub_url()
    if not url or "REPLACE_ME" in url or "example.com" in url:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO subs(name,url,enabled,created_at) VALUES (?,?,1,?)",
              ("订阅 1", url, int(time.time())))
    conn.commit()
    conn.close()


def write_config():
    """Render mihomo config.yaml from the current subscription list."""
    subs = get_subs()
    active = [s for s in subs if s["enabled"]]
    lines = [
        "port: 7890", "socks-port: 7891", "allow-lan: true",
        "bind-address: '*'", "mode: rule", "log-level: info",
        "external-controller: '0.0.0.0:9090'", f"secret: '{SECRET}'",
        "ipv6: false", "", "proxy-providers:",
    ]
    if active:
        for s in active:
            pid = f"sub_{s['id']}"
            lines += [
                f"  {pid}:",
                "    type: http",
                f"    url: '{s['url']}'",
                "    interval: 86400",
                f"    path: ./{pid}.yaml",
                "    size-limit: 0",
                "    health-check:",
                "      enable: true",
                "      interval: 1800",
                "      lazy: true",
                "      url: https://www.gstatic.com/generate_204",
            ]
    else:
        lines += ["  placeholder:", "    type: inline", "    payload: []"]

    use_all = ", ".join(f"sub_{s['id']}" for s in active) if active else "placeholder"
    lines += [
        "", "proxy-groups:",
        "  - name: 'PROXY'", "    type: select", "    proxies:",
        "      - 'AUTO'", "      - 'MANUAL'",
    ]
    for s in active:
        lines.append(f"      - 'SUB_{s['id']}'")
    lines += [
        "      - 'DIRECT'",
        "  - name: 'AUTO'", "    type: url-test",
        f"    use: [{use_all}]",
        "    url: https://www.gstatic.com/generate_204",
        "    interval: 600", "    tolerance: 100",
        "  - name: 'MANUAL'", "    type: select",
        f"    use: [{use_all}]",
    ]
    for s in active:
        lines += [
            f"  - name: 'SUB_{s['id']}'", "    type: select",
            f"    use: [sub_{s['id']}]",
        ]
    lines += ["", "rules:", "  - MATCH,PROXY", ""]

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return True


def reload_mihomo():
    code, _ = api("/configs", method="PUT", payload={"path": MIHOMO_CONFIG_PATH}, timeout=12)
    time.sleep(2.5)
    api("/proxies/PROXY", method="PUT", payload={"name": "AUTO"})
    return code in (200, 204)


def api(path, method="GET", payload=None, timeout=6):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(MIHOMO + path, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def log_event(kind, detail):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO events(ts,kind,detail) VALUES (?,?,?)",
                  (int(time.time()), kind, str(detail)))
        conn.commit()
        conn.close()


def traffic_worker():
    """Sample cumulative traffic via /connections snapshot, derive per-second rate."""
    global traffic_cache
    prev_total = None
    prev_ts = None
    while True:
        # /connections?interval=1000 is a one-shot snapshot (not a streaming endpoint)
        code, t = api("/connections?interval=1000", timeout=5)
        if code == 200 and isinstance(t.get("uploadTotal"), int):
            now = int(time.time())
            upt, dnt = t["uploadTotal"], t["downloadTotal"]
            up_rate = down_rate = 0
            if prev_total is not None and prev_ts and now > prev_ts:
                du, dd = upt - prev_total[0], dnt - prev_total[1]
                if 0 <= du < 10**12 and 0 <= dd < 10**12:
                    dt = now - prev_ts
                    up_rate, down_rate = int(du / dt), int(dd / dt)
            traffic_cache = {"up": up_rate, "down": down_rate,
                             "upTotal": upt, "downTotal": dnt, "ts": now}
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)",
                          (now, up_rate, down_rate, upt, dnt))
                if up_rate + down_rate > 50 * 1024:
                    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                    c.execute("""INSERT INTO active(day,seconds) VALUES (?,5)
                                 ON CONFLICT(day) DO UPDATE SET seconds=seconds+5""", (day,))
                conn.commit()
                conn.close()
            prev_total, prev_ts = (upt, dnt), now
        time.sleep(2)


def fmt_b(n):
    n = float(n or 0)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def usage_stats():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ts, uptotal, downtotal FROM samples ORDER BY ts")
        rows = c.fetchall()
        c.execute("SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT 40")
        events = c.fetchall()
        conn.close()

    daily, prev = {}, None
    for ts, upt, dnt in rows:
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        daily.setdefault(day, [0, 0])
        if prev is not None:
            du, dd = upt - prev[1], dnt - prev[2]
            if 0 <= du < 10**12 and 0 <= dd < 10**12:
                daily[day][0] += du
                daily[day][1] += dd
        prev = (ts, upt, dnt)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT day, seconds FROM active")
    active_days = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()

    today = datetime.now().strftime("%Y-%m-%d")
    today_up, today_down = daily.get(today, [0, 0])
    last7 = []
    for i in range(6, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        u, dn = daily.get(d, [0, 0])
        last7.append({"day": d, "up": u, "down": dn, "active": active_days.get(d, 0)})
    ev = [{"time": datetime.fromtimestamp(t).strftime("%m-%d %H:%M"),
           "kind": k, "detail": d} for t, k, d in events]
    return {"today_up": today_up, "today_down": today_down,
            "today_active": active_days.get(today, 0), "last7": last7, "events": ev}


def is_real_node(name):
    text_kw = ["剩余流量", "距离下次重置", "套餐到期", "更新软件", "更新订阅", "剩余：", "到期"]
    return not any(k in name for k in text_kw)


def provider_info():
    code, prov = api("/providers/proxies", timeout=6)
    return prov.get("providers", {}) if isinstance(prov, dict) else {}


def overview():
    code, ver = api("/version", timeout=4)
    _, cfg = api("/configs", timeout=4)
    _, proxies = api("/proxies", timeout=4)
    px = proxies.get("proxies", {}) if isinstance(proxies, dict) else {}
    pg = px.get("PROXY", {})
    now = pg.get("now", "")
    switch_on = now != "DIRECT"

    # Build provider -> node names map from each provider's own endpoint (reliable source)
    providers = provider_info()
    provider_nodes = {}
    for pid in providers:
        if not pid.startswith("sub_"):
            continue
        _, detail = api(f"/providers/proxies/{pid}", timeout=6)
        provider_nodes[pid] = [x.get("name", "") for x in (detail.get("proxies") or [])]

    name_to_provider = {}
    for pid, names in provider_nodes.items():
        for nm in names:
            # keep the first owner so duplicate node names across subs don't get overwritten
            if nm not in name_to_provider:
                name_to_provider[nm] = pid

    nodes = []
    auto = px.get("AUTO", {})
    for name in auto.get("all", []) or []:
        if not is_real_node(name):
            continue
        info = px.get(name, {})
        hist = info.get("history") or []
        delay = hist[-1].get("delay", 0) if hist else 0
        # prefer an explicit speed-test result when available
        with delay_lock:
            test_delay = delay_cache.get(name, 0)
        if test_delay:
            delay = test_delay
        alive = bool(test_delay) if test_delay else info.get("alive", False)
        nodes.append({"name": name, "alive": alive,
                      "delay": delay, "provider": name_to_provider.get(name, "")})

    subs = get_subs()
    sub_list = []
    for s in subs:
        pid = f"sub_{s['id']}"
        p = providers.get(pid, {})
        si = p.get("subscriptionInfo", {}) or {}
        sub_nodes = [n for n in provider_nodes.get(pid, []) if is_real_node(n)]
        sub_list.append({
            "id": s["id"], "name": s["name"], "url": s["url"],
            "enabled": s["enabled"], "node_count": len(sub_nodes),
            "updatedAt": p.get("updatedAt"),
            "total": si.get("Total") or si.get("total"),
            "download": si.get("Download") or si.get("download"),
            "upload": si.get("Upload") or si.get("upload"),
            "expire": si.get("Expire") or si.get("expire"),
        })

    return {"online": code == 200, "version": ver.get("version", "unknown"),
            "mode": cfg.get("mode", "unknown"), "switch_on": switch_on,
            "current": now, "node_count": len(nodes), "nodes": nodes,
            "subs": sub_list}


def validate_sub_url(url):
    if not url.startswith(("http://", "https://")):
        return False, "链接必须以 http:// 或 https:// 开头"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Clash/1.18.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(1024)
            if not body:
                return False, "订阅返回了空内容，请检查链接"
        return True, ""
    except Exception as e:
        return False, f"无法访问订阅：{e}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open("/app/index.html", "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self._send({"error": "index missing"}, 500)
            return
        if path == "/api/overview":
            self._send(overview()); return
        if path == "/api/traffic":
            self._send(traffic_cache); return
        if path == "/api/stats":
            self._send(usage_stats()); return
        if path == "/api/subs":
            self._send({"subs": overview()["subs"]}); return
        if path == "/api/health":
            self._send({"ok": True}); return
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}

        if path == "/api/switch":
            on = bool(payload.get("on"))
            target = "AUTO" if on else "DIRECT"
            code, _ = api("/proxies/PROXY", method="PUT", payload={"name": target})
            log_event("switch", "开启代理(自动选路)" if on else "关闭代理(直连)")
            self._send({"ok": code in (200, 204), "target": target}); return

        if path == "/api/node":
            name = payload.get("name", "")
            code, _ = api("/proxies/MANUAL", method="PUT", payload={"name": name})
            api("/proxies/PROXY", method="PUT", payload={"name": "MANUAL"})
            log_event("node", name)
            self._send({"ok": code in (200, 204), "node": name}); return

        if path == "/api/auto":
            code, _ = api("/proxies/PROXY", method="PUT", payload={"name": "AUTO"})
            log_event("switch", "切换为自动选路")
            self._send({"ok": code in (200, 204)}); return

        if path == "/api/speedtest":
            from urllib.parse import quote
            url = quote(TEST_URL, safe="")
            code, result = api(f"/group/AUTO/delay?url={url}&timeout=8000", timeout=120)
            tested = 0
            if code == 200 and isinstance(result, dict):
                with delay_lock:
                    delay_cache.clear()
                    for nm, d in result.items():
                        if isinstance(d, int) and d > 0:
                            delay_cache[nm] = d
                            tested += 1
            log_event("speedtest", f"节点测速完成，{tested} 个节点有响应")
            self._send({"ok": code == 200, "tested": tested}); return

        if path == "/api/refresh":
            sid = payload.get("id")
            if sid:
                code, _ = api(f"/providers/proxies/sub_{sid}", method="PUT", payload={})
            else:
                code = 200
                for s in get_subs():
                    if s["enabled"]:
                        api(f"/providers/proxies/sub_{s['id']}", method="PUT", payload={})
            log_event("refresh", "更新订阅" + (f" #{sid}" if sid else "(全部)"))
            self._send({"ok": code in (200, 204)}); return

        if path == "/api/subs/add":
            name = str(payload.get("name", "")).strip() or "新订阅"
            url = str(payload.get("url", "")).strip()
            ok, msg = validate_sub_url(url)
            if not ok:
                self._send({"ok": False, "error": msg}); return
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO subs(name,url,enabled,created_at) VALUES (?,?,1,?)",
                          (name, url, int(time.time())))
                conn.commit()
                conn.close()
            write_config()
            reload_ok = reload_mihomo()
            log_event("sub", f"添加订阅：{name}")
            self._send({"ok": True, "reloaded": reload_ok}); return

        if path == "/api/subs/delete":
            sid = payload.get("id")
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT name FROM subs WHERE id=?", (sid,))
                row = c.fetchone()
                c.execute("DELETE FROM subs WHERE id=?", (sid,))
                conn.commit()
                conn.close()
            write_config()
            reload_mihomo()
            log_event("sub", f"删除订阅：{row[0] if row else sid}")
            self._send({"ok": True}); return

        if path == "/api/subs/toggle":
            sid = payload.get("id")
            enabled = bool(payload.get("enabled"))
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("UPDATE subs SET enabled=? WHERE id=?", (1 if enabled else 0, sid))
                conn.commit()
                conn.close()
            write_config()
            reload_mihomo()
            log_event("sub", ("启用" if enabled else "禁用") + f"订阅 #{sid}")
            self._send({"ok": True}); return

        self._send({"error": "not found"}, 404)


if __name__ == "__main__":
    init_db()
    migrate_subs()
    write_config()
    reload_mihomo()
    threading.Thread(target=traffic_worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    print("dashboard listening on 0.0.0.0:8000", flush=True)
    srv.serve_forever()
