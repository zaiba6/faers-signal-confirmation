"""Polite, cached, robots-aware fetcher for the label-source collection.

- Redirects are followed MANUALLY; every hop's host gets its robots.txt fetched (and cached in data/raw/labels/robots_<host>.txt)
  and the hop is refused (RobotsDisallowed) if our UA may not fetch that path.
- >= 30 s between any two requests to the same host (or the host's Crawl-delay/Hit-rate if larger).
- Optional time gate (e.g. accessdata: only 23:00-05:00 America/New_York); sleeps until the window opens.
- Never re-fetches: if `out` exists the cached file is returned. Writes <out>.meta.json (request chain, final URL, status, time).
"""
import json, re, time, datetime as dt
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/labels"
UA = "faers-signal-confirmation-research/0.1 (academic research; amla.z@northeastern.edu)"
MIN_DELAY = 30
_last = {}          # host -> monotonic time of last request
_robots = {}        # host -> (RobotFileParser|None, delay)


class RobotsDisallowed(Exception):
    pass


def _gate(window):
    """window = (start_hour, end_hour, tz): sleep until now is inside it (handles wrap past midnight)."""
    if not window: return
    s, e, tz = window
    while True:
        h = dt.datetime.now(ZoneInfo(tz)).hour
        if (s <= h or h < e) if s > e else (s <= h < e): return
        print(f"[gate] outside {s}:00-{e}:00 {tz}; sleeping 300 s", flush=True)
        time.sleep(300)


def _wait(host, delay):
    t = _last.get(host)
    if t is not None:
        w = delay - (time.monotonic() - t)
        if w > 0: time.sleep(w)


def _http(sess, method, url, host, delay, window, **kw):
    _gate(window)
    _wait(host, delay)
    try:
        return sess.request(method, url, headers={"User-Agent": UA}, allow_redirects=False, timeout=90, **kw)
    finally:
        _last[host] = time.monotonic()


def robots_for(sess, host, scheme="https", window=None):
    if host in _robots: return _robots[host]
    f = RAW / f"robots_{host}.txt"
    if not f.exists():
        r = _http(sess, "GET", f"{scheme}://{host}/robots.txt", host, MIN_DELAY, window)
        f.write_text(r.text if r.status_code == 200 else f"# HTTP {r.status_code} (no robots.txt)\n")
    txt = f.read_text()
    rp, delay = None, MIN_DELAY
    if not txt.startswith("# HTTP"):
        rp = RobotFileParser(); rp.parse(txt.splitlines())
        cd = rp.crawl_delay(UA)
        m = re.search(r"(?im)^\s*(?:crawl-delay|hit-rate)\s*:\s*(\d+)", txt)
        delay = max(MIN_DELAY, int(cd or 0), int(m.group(1)) if m else 0)
    _robots[host] = (rp, delay)
    return _robots[host]


def fetch(url, out, method="GET", data=None, sess=None, window=None, max_hops=6):
    """Return Path of saved body. Raises RobotsDisallowed on any disallowed hop."""
    out = Path(out)
    if out.exists() and out.stat().st_size > 0:
        return out
    sess = sess or requests.Session()
    chain, cur = [], url
    for _ in range(max_hops):
        p = urlparse(cur)
        rp, delay = robots_for(sess, p.netloc, p.scheme, window)
        path = p.path + ("?" + p.query if p.query else "")
        if rp is not None and not rp.can_fetch(UA, cur):
            raise RobotsDisallowed(f"{cur} disallowed by robots.txt of {p.netloc}")
        r = _http(sess, method, cur, p.netloc, delay, window, data=data if method == "POST" else None)
        chain.append(dict(url=cur, status=r.status_code, location=r.headers.get("location")))
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            cur = urljoin(cur, r.headers["location"])
            if r.status_code in (301, 302, 303): method, data = "GET", None
            continue
        meta = dict(requested_url=url, final_url=cur, status=r.status_code, chain=chain,
                    fetched_at=dt.datetime.now(dt.timezone.utc).isoformat(), bytes=len(r.content), post_data=data)
        if r.status_code != 200:
            Path(str(out) + ".failed.json").write_text(json.dumps(meta, indent=1))
            raise RuntimeError(f"HTTP {r.status_code} for {cur}")
        out.write_bytes(r.content)
        Path(str(out) + ".meta.json").write_text(json.dumps(meta, indent=1))
        return out
    raise RuntimeError("too many redirects: " + url)
