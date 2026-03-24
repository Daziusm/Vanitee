#!/usr/bin/env python3
"""
Vanitee — Discord Vanity Code Checker
Zero-flicker Textual TUI · Adaptive rate limiting · Proxy pool support
Multi-instance coordination · Discord webhook notifications

No API bypass — clean, IP-rate-limited approach.

Quick start:
    pip install -r requirements.txt
    python vanitee.py wordlist.txt

Multi-instance usage (each window gets a non-overlapping slice):
    python vanitee.py wordlist.txt   # enable "Multiple windows" in config
    python vanitee.py wordlist.txt   # second terminal, same wordlist
"""

import asyncio
import os
import sys
import time
import json
import argparse
from collections import deque
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── Dependency checks ─────────────────────────────────────────────────────────
_missing = []
try:
    import httpx
except ImportError:
    _missing.append("httpx")

try:
    from textual.app import App, ComposeResult
    from textual.screen import Screen
    from textual.binding import Binding
    from textual.widgets import (
        Header, Footer, ListView, ListItem, Label,
        Button, Static, RichLog, Input, Checkbox, Collapsible,
    )
    from textual.containers import Vertical, Horizontal, Container
    from textual import on
except ImportError:
    _missing.append("textual")

if _missing:
    sys.exit(f"Missing dependencies: pip install {' '.join(_missing)}")

try:
    import h2  # noqa: F401
    _USE_HTTP2 = True
except ImportError:
    _USE_HTTP2 = False

# ── Constants ─────────────────────────────────────────────────────────────────
DISCORD_API  = "https://discord.com/api/v10/invites/{}"
PROGRESS_EXT = ".progress"
COORD_EXT    = ".coord"
LOCK_EXT     = ".coord.lock"
CHUNK_SIZE       = 500   # codes per coordinator chunk
STALE_SECS       = 120   # seconds before an uncompleted claimed chunk is reclaimable
PROXY_TEST_URL   = "https://discord.com/api/v10/invites/a"
CONFIG_PATH      = Path("config.json")
PROXY_TEST_SECS  = 6.0
PROXY_TEST_CONC  = 25    # max concurrent proxy tests
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
# ── Persistent config (config.json) ──────────────────────────────────────────

_CONFIG_DEFAULTS = {
    "rps":             2,
    "output":          "hits.txt",
    "proxy_file":      "",
    "webhook_url":     "",
    "webhook_max_len": 3,
    "coordinate":      False,
}


def load_app_config() -> dict:
    """Load config.json, merging with defaults for any missing keys."""
    cfg = dict(_CONFIG_DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_app_config(cfg: dict) -> None:
    """Write only the persistent fields to config.json."""
    keys = _CONFIG_DEFAULTS.keys()
    try:
        CONFIG_PATH.write_text(
            json.dumps({k: cfg[k] for k in keys if k in cfg},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


_HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


# ══════════════════════════════════════════════════════════════════════════════
# Checker logic  (no UI dependencies)
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """Adaptive token-bucket — backs off on 429, recovers gradually."""

    def __init__(self, rps: float):
        self.target_rps = rps
        self._interval  = 1.0 / rps
        self._last      = 0.0
        self._lock      = asyncio.Lock()
        self._recovery: Optional[asyncio.Task] = None

    async def acquire(self):
        async with self._lock:
            now  = time.monotonic()
            wait = max(0.0, self._last + self._interval - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    def on_429(self, retry_after: float):
        self._interval = min(self._interval * 1.8, 5.0)
        if self._recovery is None or self._recovery.done():
            self._recovery = asyncio.create_task(self._recover(retry_after))

    async def _recover(self, initial_wait: float):
        await asyncio.sleep(initial_wait + 1.0)
        target = 1.0 / self.target_rps
        while self._interval > target * 1.05:
            self._interval = max(target, self._interval * 0.90)
            await asyncio.sleep(3.0)


class Stats:
    def __init__(self):
        self.requests   = 0
        self.hits       = 0
        self.taken      = 0
        self.errors     = 0
        self.retries    = 0
        self.skipped_rl = 0          # codes skipped purely due to rate limiting
        self.start      = time.time()
        self.peak_rps   = 0
        self._ts: deque        = deque(maxlen=60)
        self._recent_429: deque = deque(maxlen=20)  # last 20 outcomes: True=429

    def record(self, was_429: bool = False):
        self.requests += 1
        self._ts.append(time.time())
        self._recent_429.append(was_429)
        r = self.live_rps
        if r > self.peak_rps:
            self.peak_rps = r

    @property
    def is_rate_limited(self) -> bool:
        """True when the last several outcomes are almost all 429s."""
        if len(self._recent_429) < 5:
            return False
        return sum(self._recent_429) / len(self._recent_429) >= 0.7

    @property
    def live_rps(self) -> int:
        now = time.time()
        return sum(1 for t in self._ts if now - t <= 1.0)

    @property
    def avg_rps(self) -> float:
        s = time.time() - self.start
        return self.requests / s if s > 0 else 0.0

    @property
    def hit_rate(self) -> float:
        n = self.hits + self.taken
        return (self.hits / n * 100) if n > 0 else 0.0

    @property
    def elapsed(self) -> str:
        s = int(time.time() - self.start)
        return f"{s // 3600:02}:{(s % 3600) // 60:02}:{s % 60:02}"

    def eta(self, remaining: int) -> str:
        rps = self.avg_rps
        if rps <= 0 or remaining <= 0:
            return "--:--:--"
        s = int(remaining / rps)
        return f"{s // 3600:02}:{(s % 3600) // 60:02}:{s % 60:02}"


class Entry:
    def __init__(self, code: str, status: str, info: str = ""):
        self.code   = code
        self.status = status
        self.info   = info
        self.ts     = datetime.now().strftime("%H:%M:%S")


# ── Wordlist coordinator (multi-instance chunk claiming) ──────────────────────

class WordlistCoordinator:
    """
    Splits a wordlist into CHUNK_SIZE chunks.  Multiple processes each claim
    chunks via a lock file, ensuring no two processes work on the same codes.

    Files created alongside the wordlist:
      <wordlist>.coord      — JSON chunk registry (free / claimed / done)
      <wordlist>.coord.lock — ephemeral lock file (removed after each operation)
    """

    def __init__(self, wordlist_path: Path, total_codes: int):
        self.coord_path = wordlist_path.with_suffix(COORD_EXT)
        self.lock_path  = wordlist_path.with_suffix(LOCK_EXT)
        self.total      = total_codes
        self._pid       = str(os.getpid())

    def initialize(self):
        """Create the coord file if it doesn't exist yet."""
        if not self._lock():
            return
        try:
            if not self.coord_path.exists():
                chunks = [
                    {"s": i, "e": min(i + CHUNK_SIZE, self.total),
                     "st": "free", "pid": None, "at": None}
                    for i in range(0, self.total, CHUNK_SIZE)
                ]
                self._write({"chunks": chunks})
        finally:
            self._unlock()

    def claim_next(self) -> Optional[tuple]:
        """Atomically claim the next free (or stale) chunk. Returns (start, end)."""
        if not self._lock():
            return None
        try:
            data = self._read()
            if data is None:
                return None
            now = time.time()
            for chunk in data["chunks"]:
                if chunk["st"] == "free":
                    chunk.update(st="claimed", pid=self._pid, at=now)
                    self._write(data)
                    return (chunk["s"], chunk["e"])
                if chunk["st"] == "claimed" and (now - (chunk.get("at") or 0)) > STALE_SECS:
                    chunk.update(st="claimed", pid=self._pid, at=now)
                    self._write(data)
                    return (chunk["s"], chunk["e"])
            return None
        except Exception:
            return None
        finally:
            self._unlock()

    def mark_done(self, start: int, end: int):
        if not self._lock():
            return
        try:
            data = self._read()
            if data is None:
                return
            for chunk in data["chunks"]:
                if chunk["s"] == start and chunk["e"] == end:
                    chunk.update(st="done", pid=None, at=None)
                    break
            self._write(data)
        except Exception:
            pass
        finally:
            self._unlock()

    def release_mine(self):
        """Mark all chunks claimed by this PID back to free (called on exit)."""
        if not self._lock():
            return
        try:
            data = self._read()
            if data is None:
                return
            for chunk in data["chunks"]:
                if chunk["st"] == "claimed" and chunk.get("pid") == self._pid:
                    chunk.update(st="free", pid=None, at=None)
            self._write(data)
        except Exception:
            pass
        finally:
            self._unlock()

    def global_stats(self) -> dict:
        try:
            data = self._read()
            if data is None:
                return {}
            chunks = data["chunks"]
            return {
                "total":   len(chunks),
                "done":    sum(1 for c in chunks if c["st"] == "done"),
                "claimed": sum(1 for c in chunks if c["st"] == "claimed"),
                "free":    sum(1 for c in chunks if c["st"] == "free"),
            }
        except Exception:
            return {}

    # ── internals ─────────────────────────────────────────────────────────────

    def _read(self) -> Optional[dict]:
        try:
            return json.loads(self.coord_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write(self, data: dict):
        self.coord_path.write_text(
            json.dumps(data, separators=(",", ":")),
            encoding="utf-8",
        )

    def _lock(self, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, self._pid.encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 10:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                time.sleep(0.05)
        return False

    def _unlock(self):
        try:
            self.lock_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def count_lines(p: Path) -> int:
    try:
        data = p.read_bytes()
        n = data.count(b"\n")
        return n + (1 if data and not data.endswith(b"\n") else 0)
    except Exception:
        return 0


def load_proxies(path: Path) -> list:
    """Return non-empty, non-comment lines from a proxy file."""
    if not path.exists():
        return []
    return [
        l.strip() for l in path.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]


def load_checkpoint(path: Path) -> set:
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")).get("checked", []))
        except Exception:
            pass
    return set()


def save_checkpoint(path: Path, checked: set):
    try:
        path.write_text(
            json.dumps({"checked": list(checked), "saved": datetime.now().isoformat()},
                       separators=(",", ":")),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── Worker (blocking queue with None sentinel) ────────────────────────────────

async def _checker_worker(
    client:          "httpx.AsyncClient",
    limiter:         RateLimiter,
    queue:           asyncio.Queue,
    stats:           Stats,
    feed:            list,
    out_file,
    checked:         set,
    cp_path:         Optional[Path],
    webhook_url:     Optional[str] = None,
    webhook_max_len: int           = 0,    # 0 = notify for every hit
    max_retry:       int           = 5,
):
    while True:
        code = await queue.get()   # blocks until an item or sentinel arrives

        if code is None:           # stop sentinel — exit cleanly
            queue.task_done()
            return

        try:
            rl_attempts  = 0
            rl_skipped   = False
            for attempt in range(max_retry):
                await limiter.acquire()
                try:
                    resp = await client.get(
                        DISCORD_API.format(code),
                        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                    )

                    if resp.status_code == 200:
                        data    = resp.json()
                        guild   = data.get("guild", {}).get("name", "?")
                        members = data.get("approximate_member_count", "?")
                        mem_str = f"{members:,}" if isinstance(members, int) else str(members)
                        stats.record(was_429=False)
                        stats.taken += 1
                        feed.append(Entry(code, "TAKEN", f"{guild} ({mem_str} members)"))
                        break

                    if resp.status_code == 404:
                        body = resp.json()
                        stats.record(was_429=False)
                        if body.get("code") == 10006:
                            stats.hits += 1
                            feed.append(Entry(code, "HIT", "available"))
                            if out_file:
                                out_file.write(code + "\n")
                                out_file.flush()
                            if webhook_url and (
                                webhook_max_len == 0 or len(code) <= webhook_max_len
                            ):
                                asyncio.create_task(send_webhook(webhook_url, code))
                        else:
                            stats.errors += 1
                            feed.append(Entry(code, "ERR", f"discord err {body.get('code')}"))
                        break

                    if resp.status_code == 429:
                        body        = resp.json()
                        retry_after = float(body.get("retry_after", 2.0))
                        stats.record(was_429=True)
                        stats.retries += 1
                        limiter.on_429(retry_after)
                        if rl_attempts >= 1:
                            # Waited once already — skip this code rather than
                            # burning another 30 s.  Not added to `checked` so
                            # --resume will pick it up in a later run.
                            stats.skipped_rl += 1
                            rl_skipped = True
                            feed.append(Entry(
                                code, "ERR",
                                f"rate limited ×2 — skipped (resume will retry)",
                            ))
                            break
                        rl_attempts += 1
                        feed.append(Entry(
                            code, "ERR",
                            f"429 — waiting {retry_after:.0f}s, one retry…",
                        ))
                        await asyncio.sleep(retry_after)
                        continue

                    stats.record(was_429=False)
                    stats.errors += 1
                    feed.append(Entry(code, "ERR", f"HTTP {resp.status_code}"))
                    break

                except httpx.TimeoutException:
                    stats.record(); stats.errors += 1
                    feed.append(Entry(code, "ERR", "timed out"))
                    break
                except httpx.ConnectError:
                    stats.record(); stats.errors += 1
                    feed.append(Entry(code, "ERR", "connection error"))
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    stats.record(); stats.errors += 1
                    feed.append(Entry(code, "ERR", str(ex)[:70]))
                    break
            else:
                stats.errors += 1
                feed.append(Entry(code, "ERR", "max retries — skipped"))

            if not rl_skipped:
                checked.add(code)
                if cp_path and len(checked) % 500 == 0:
                    save_checkpoint(cp_path, checked)
        finally:
            queue.task_done()


# ── Discord webhook notifications ────────────────────────────────────────────

# Embed color by code length  (shorter = rarer = more dramatic color)
_WEBHOOK_COLORS = {1: 0xFF0000, 2: 0xFF2200, 3: 0xFF8C00, 4: 0xF1C40F, 5: 0x2ECC71}
_WEBHOOK_DEFAULT_COLOR = 0x2ECC71


async def send_webhook(webhook_url: str, code: str) -> None:
    """Fire a Discord embed for a vanity hit. Silently swallows all errors."""
    n     = len(code)
    color = _WEBHOOK_COLORS.get(n, _WEBHOOK_DEFAULT_COLOR)
    rarity = {2: "🔴 ULTRA RARE", 3: "🟠 RARE", 4: "🟡 Uncommon"}.get(n, "🟢 Hit")

    payload = {
        "username":   "DC-Checker 🔥",
        "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        "embeds": [{
            "title":       f"🔥  discord.gg/{code}  is free!",
            "url":         f"https://discord.gg/{code}",
            "description": f"**[discord.gg/{code}](https://discord.gg/{code})**",
            "color":       color,
            "fields": [
                {"name": "Code",    "value": f"`{code}`",         "inline": True},
                {"name": "Length",  "value": f"{n} letters",      "inline": True},
                {"name": "Rarity",  "value": rarity,              "inline": True},
            ],
            "footer":    {"text": "DC-Checker • vanity sniper"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }],
    }
    # @here ping for 2–3 letter codes so friends notice immediately
    if n <= 3:
        payload["content"] = f" **{n}-letter vanity found!**"

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                webhook_url, json=payload,
                timeout=httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0),
            )
    except Exception:
        pass  # never crash the checker over a webhook failure


# ── Proxy health test (standalone coroutine) ──────────────────────────────────

async def _test_one_proxy(url: Optional[str], sem: asyncio.Semaphore) -> tuple:
    """Returns (url, ok, latency_ms, error_str)."""
    async with sem:
        t0 = time.time()
        kw: dict = {"follow_redirects": True}
        if url:
            kw["proxy"] = url
        try:
            async with httpx.AsyncClient(**kw) as client:
                await client.get(
                    PROXY_TEST_URL,
                    timeout=httpx.Timeout(
                        connect=PROXY_TEST_SECS, read=PROXY_TEST_SECS,
                        write=5.0, pool=5.0,
                    ),
                )
            return (url, True, int((time.time() - t0) * 1000), "")
        except httpx.TimeoutException:
            return (url, False, None, "timed out")
        except httpx.ConnectError:
            return (url, False, None, "connection refused")
        except Exception as exc:
            return (url, False, None, str(exc)[:50])


# ══════════════════════════════════════════════════════════════════════════════
# Screen 1 — File Picker
# ══════════════════════════════════════════════════════════════════════════════

class FilePickerScreen(Screen):
    BINDINGS = [
        Binding("q",      "app.quit", "Quit"),
        Binding("escape", "app.quit", "Quit"),
    ]

    DEFAULT_CSS = """
    FilePickerScreen {
        align: center middle;
        background: #090912;
    }
    #picker-box {
        width: 74;
        height: auto;
        border: double magenta;
        padding: 1 3;
        background: #090912;
    }
    #picker-title {
        text-align: center;
        color: magenta;
        text-style: bold;
        padding-bottom: 1;
    }
    #picker-sub {
        text-align: center;
        color: #555;
        padding-bottom: 1;
    }
    #file-list {
        height: auto;
        max-height: 16;
        background: #090912;
    }
    ListItem {
        padding: 0 1;
        color: #ccc;
    }
    ListItem.--highlight {
        background: #2a0a48;
        color: white;
    }
    #no-files {
        text-align: center;
        color: yellow;
        padding: 1 0;
    }
    #picker-hint {
        text-align: center;
        color: #3a3a5a;
        padding-top: 1;
    }
    """

    def __init__(self, txt_files: list):
        super().__init__()
        self.txt_files = txt_files

    def compose(self) -> ComposeResult:
        with Container(id="picker-box"):
            yield Static("⚡  DC-Checker", id="picker-title")
            yield Static("Select a wordlist to check", id="picker-sub")
            if self.txt_files:
                items = [
                    ListItem(Label(f"  {f.name:<44}  {count_lines(f):>12,} lines"))
                    for f in self.txt_files
                ]
                yield ListView(*items, id="file-list")
            else:
                yield Static(
                    "No .txt files found in the current directory.\n"
                    "Run: python vanity_checker.py <wordlist.txt>",
                    id="no-files",
                )
            yield Static("↑ ↓  Navigate    Enter  Select    Q  Quit", id="picker-hint")

    @on(ListView.Selected)
    def file_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self.txt_files):
            self.app.push_screen(ConfigScreen(self.txt_files[idx]))


# ══════════════════════════════════════════════════════════════════════════════
# Screen 2 — Configure  (simplified — no confusing advanced fields up front)
# ══════════════════════════════════════════════════════════════════════════════

class ConfigScreen(Screen):
    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("f5",     "start",   "Start"),
    ]

    DEFAULT_CSS = """
    ConfigScreen {
        align: center middle;
        background: #090912;
    }
    #config-box {
        width: 70;
        height: auto;
        border: double magenta;
        padding: 1 3;
        background: #090912;
    }
    #config-title {
        text-align: center;
        color: magenta;
        text-style: bold;
    }
    #config-file {
        text-align: center;
        color: #555;
        padding-bottom: 1;
    }
    #preview-bar {
        text-align: center;
        height: 1;
        color: cyan;
        margin-bottom: 1;
    }
    .row { height: 3; }
    .lbl {
        width: 22;
        height: 3;
        content-align: left middle;
        color: #888;
        padding: 1 0;
    }
    Input {
        width: 1fr;
        background: #0d0d22;
        border: tall #333;
        color: white;
    }
    Input:focus { border: tall magenta; }
    .toggle-row { height: 3; }
    .toggle-lbl {
        width: 1fr;
        height: 3;
        content-align: left middle;
        color: #888;
        padding: 1 0;
    }
    Checkbox {
        background: #090912;
        border: none;
        height: 3;
    }
    Collapsible {
        background: #090912;
        border: none;
        padding: 0;
        margin-top: 0;
    }
    CollapsibleTitle {
        color: #444;
        padding: 0;
    }
    .rule { height: 1; color: #1a1a2e; margin: 1 0; }
    #btn-row {
        height: 3;
        margin-top: 2;
        align: center middle;
    }
    #btn-start { margin: 0 1; background: magenta; color: white; border: none; }
    #btn-back  { margin: 0 1; background: #1a1a1a; color: #666; border: none; }
    #help-bar  { text-align: center; height: 1; color: #333; margin-top: 1; }
    """

    _HELP = {
        "inp-rps":     "Requests per second per proxy. Keep at 1–2 to stay safe.",
        "inp-output":  "File where available codes are saved. Created automatically.",
        "inp-proxies": "Your proxy list file (http_proxies.txt, one URL per line).",
        "inp-minlen":  "Skip codes shorter than this. e.g. 3 = only check 3+ character codes.",
        "inp-maxlen":  "Skip codes longer than this.  e.g. 8 = only check up to 8 characters.",
        "chk-resume":  "Pick up where you left off — skips codes you already checked.",
        "chk-append":  "Add new hits to the output file instead of replacing it.",
        "chk-coord":       "Open multiple windows on the same wordlist without them overlapping.",
        "inp-webhook":     "Discord webhook URL — sends an embed + @here ping to your server when a vanity is found.",
        "inp-webhook-len": "Only ping for codes this short or shorter. 3 = 2 and 3 letter codes. 0 = every hit.",
    }

    def __init__(self, wordlist_path: Path):
        super().__init__()
        self.wordlist_path = wordlist_path
        self._total        = count_lines(wordlist_path)
        self._cfg          = load_app_config()

        # Override proxy_file from config only if that file actually exists;
        # otherwise fall back to auto-detecting known filenames.
        saved_proxy = self._cfg.get("proxy_file", "")
        if saved_proxy and Path(saved_proxy).exists():
            self._default_proxies = saved_proxy
        elif Path("http_proxies.txt").exists():
            self._default_proxies = "http_proxies.txt"
        elif Path("proxies.txt").exists():
            self._default_proxies = "proxies.txt"
        else:
            self._default_proxies = ""

        self._default_resume = Path("hits.progress").exists()
        self._default_coord  = (
            self._cfg.get("coordinate", False)
            or wordlist_path.with_suffix(COORD_EXT).exists()
        )

    def compose(self) -> ComposeResult:
        with Container(id="config-box"):
            yield Static("⚡  DC-Checker  ·  Setup", id="config-title")
            yield Static(f"{self.wordlist_path.name}  ({self._total:,} codes)", id="config-file")
            yield Static("", id="preview-bar")

            # ── Core (always visible) ─────────────────────────────────────────
            with Horizontal(classes="row"):
                yield Label("Speed  (req/sec)", classes="lbl")
                yield Input(str(self._cfg.get("rps", 2)), id="inp-rps",
                            placeholder="1–2 is safe with proxies")
            with Horizontal(classes="row"):
                yield Label("Save hits to", classes="lbl")
                yield Input(self._cfg.get("output", "hits.txt"), id="inp-output")
            with Horizontal(classes="row"):
                yield Label("Proxy file", classes="lbl")
                yield Input(self._default_proxies, id="inp-proxies",
                            placeholder="optional — e.g. http_proxies.txt")

            # ── Webhook ───────────────────────────────────────────────────────
            yield Static("─" * 56, classes="rule")
            with Horizontal(classes="row"):
                yield Label("Discord webhook", classes="lbl")
                yield Input(self._cfg.get("webhook_url", ""), id="inp-webhook",
                            placeholder="https://discord.com/api/webhooks/…  (paste in config.json)")
            with Horizontal(classes="row"):
                yield Label("Notify for codes ≤", classes="lbl")
                yield Input(str(self._cfg.get("webhook_max_len", 3)), id="inp-webhook-len",
                            placeholder="3 = only 2–3 letter codes  ·  0 = all hits")

            # ── Simple toggles ────────────────────────────────────────────────
            yield Static("─" * 56, classes="rule")
            with Horizontal(classes="toggle-row"):
                yield Label("Continue previous run", classes="toggle-lbl")
                yield Checkbox("", id="chk-resume", value=self._default_resume)
            with Horizontal(classes="toggle-row"):
                yield Label("Multiple windows (no overlap)", classes="toggle-lbl")
                yield Checkbox("", id="chk-coord", value=self._default_coord)

            # ── Advanced (collapsed by default) ───────────────────────────────
            with Collapsible(title="Advanced options", collapsed=True):
                with Horizontal(classes="row"):
                    yield Label("Min code length", classes="lbl")
                    yield Input("", id="inp-minlen", placeholder="e.g. 3  (leave blank = check all)")
                with Horizontal(classes="row"):
                    yield Label("Max code length", classes="lbl")
                    yield Input("", id="inp-maxlen", placeholder="e.g. 8  (leave blank = check all)")
                with Horizontal(classes="toggle-row"):
                    yield Label("Append to output file", classes="toggle-lbl")
                    yield Checkbox("", id="chk-append", value=False)

            with Horizontal(id="btn-row"):
                yield Button("▶  Start", id="btn-start", variant="primary")
                yield Button("←  Back",  id="btn-back")
            yield Static("F5 Start    Escape Back    Tab navigate", id="help-bar")

    def on_mount(self) -> None:
        self._update_preview()

    @on(Input.Changed)
    def _on_input_changed(self, _e) -> None:
        self._update_preview()

    def on_input_focus(self, event) -> None:
        wid = event.input.id or ""
        self.query_one("#help-bar", Static).update(
            f"[dim]{self._HELP.get(wid, '')}[/]"
        )

    def _update_preview(self) -> None:
        try:
            rps = float(self.query_one("#inp-rps", Input).value or "2")
        except ValueError:
            rps = 2.0

        proxy_val = self.query_one("#inp-proxies", Input).value.strip()
        n_proxies = 0
        if proxy_val:
            p = Path(proxy_val)
            if p.exists():
                n_proxies = sum(
                    1 for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                )

        if n_proxies > 0:
            preview = (
                f"{n_proxies} proxies  ×  {rps:.0f} req/s"
                f"  =  [bold cyan]{rps * n_proxies:.0f} req/s total[/]"
                f"  [dim]·  test them before starting![/]"
            )
        else:
            preview = f"Direct connection  ·  [bold cyan]{rps:.0f} req/s[/]"

        self.query_one("#preview-bar", Static).update(preview)

    def _collect(self) -> dict:
        def _f(wid, d):
            try:
                return float(self.query_one(wid, Input).value or str(d))
            except ValueError:
                return d

        def _oi(wid):
            v = self.query_one(wid, Input).value.strip()
            return int(v) if v.isdigit() else None

        def _cb(wid):
            return self.query_one(wid, Checkbox).value

        proxy_val   = self.query_one("#inp-proxies", Input).value.strip()
        webhook_val = self.query_one("#inp-webhook", Input).value.strip()
        try:
            wh_len = int(self.query_one("#inp-webhook-len", Input).value.strip() or "3")
        except ValueError:
            wh_len = 3
        return dict(
            rps             = _f("#inp-rps", 2.0),
            output          = self.query_one("#inp-output", Input).value.strip() or "hits.txt",
            proxy_file      = proxy_val if proxy_val else None,
            min_len         = _oi("#inp-minlen"),
            max_len         = _oi("#inp-maxlen"),
            resume          = _cb("#chk-resume"),
            append          = _cb("#chk-append"),
            coordinate      = _cb("#chk-coord"),
            webhook_url     = webhook_val if webhook_val else None,
            webhook_max_len = wh_len,
        )

    @on(Button.Pressed, "#btn-start")
    def _start(self) -> None:
        cfg = self._collect()
        save_app_config(cfg)          # persist for next launch
        proxy_val = cfg.get("proxy_file")
        if proxy_val:
            proxies = load_proxies(Path(proxy_val))
            if proxies:
                self.app.push_screen(
                    ProxyCheckerScreen(proxies, cfg, self.wordlist_path)
                )
                return
        self.app.push_screen(CheckerScreen(self.wordlist_path, **cfg))

    @on(Button.Pressed, "#btn-back")
    def _back(self) -> None:
        self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_start(self) -> None:
        cfg = self._collect()
        save_app_config(cfg)
        proxy_val = cfg.get("proxy_file")
        if proxy_val:
            proxies = load_proxies(Path(proxy_val))
            if proxies:
                self.app.push_screen(
                    ProxyCheckerScreen(proxies, cfg, self.wordlist_path)
                )
                return
        self.app.push_screen(CheckerScreen(self.wordlist_path, **cfg))


# ══════════════════════════════════════════════════════════════════════════════
# Screen 3 — Proxy Health Check
# ══════════════════════════════════════════════════════════════════════════════

class ProxyCheckerScreen(Screen):
    """Tests every proxy against Discord. Shows live pass/fail. Start with clean list."""

    BINDINGS = [Binding("escape", "go_back", "Back")]

    DEFAULT_CSS = """
    ProxyCheckerScreen {
        align: center middle;
        background: #090912;
    }
    #pc-box {
        width: 74;
        height: auto;
        max-height: 46;
        border: double magenta;
        padding: 1 3;
        background: #090912;
    }
    #pc-title {
        text-align: center;
        color: magenta;
        text-style: bold;
    }
    #pc-sub {
        text-align: center;
        color: #555;
        padding-bottom: 1;
    }
    #pc-bar    { height: 1; color: cyan; }
    #pc-counts { height: 1; margin-bottom: 1; }
    RichLog {
        height: 16;
        border: round #1a1a2e;
        background: #060610;
        padding: 0 1;
    }
    #pc-btn-row {
        height: 3;
        margin-top: 1;
        align: center middle;
    }
    #btn-proceed {
        margin: 0 1;
        background: magenta;
        color: white;
        border: none;
    }
    #btn-pc-back {
        margin: 0 1;
        background: #1a1a1a;
        color: #666;
        border: none;
    }
    #pc-hint {
        text-align: center;
        color: #2a2a4a;
        height: 1;
        margin-top: 1;
    }
    """

    def __init__(self, proxy_urls: list, config: dict, wordlist_path: Path):
        super().__init__()
        self.proxy_urls   = proxy_urls
        self.config       = config
        self.wordlist_path = wordlist_path

        self._results: list  = []   # (url, ok, ms, error)
        self._log_cursor     = 0
        self._done           = False
        self._notified_done  = False
        self._test_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        total = len(self.proxy_urls)
        with Container(id="pc-box"):
            yield Static("⚡  Proxy Health Check", id="pc-title")
            yield Static(
                f"Testing {total} proxies against Discord — this takes ~{PROXY_TEST_SECS:.0f}s ...",
                id="pc-sub",
            )
            yield Static("", id="pc-bar")
            yield Static("", id="pc-counts")
            yield RichLog(id="pc-log", markup=True, highlight=False,
                          wrap=False, auto_scroll=True)
            with Horizontal(id="pc-btn-row"):
                yield Button("▶  Start with 0 working", id="btn-proceed", disabled=True)
                yield Button("💾  Save list", id="btn-save", disabled=True)
                yield Button("←  Back", id="btn-pc-back")
            yield Static(
                "Results stream in as they finish — fast proxies appear first",
                id="pc-hint",
            )

    async def on_mount(self) -> None:
        self._test_task = asyncio.create_task(self._run_tests())
        self.set_interval(0.15, self._refresh_ui)

    async def _run_tests(self) -> None:
        sem = asyncio.Semaphore(PROXY_TEST_CONC)
        tasks = [
            asyncio.create_task(_test_one_proxy(url, sem))
            for url in self.proxy_urls
        ]
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                self._results.append(result)
            except Exception:
                pass
        self._done = True

    def _refresh_ui(self) -> None:
        try:
            total   = len(self.proxy_urls)
            tested  = len(self._results)
            working = sum(1 for _, ok, _, _ in self._results if ok)
            dead    = tested - working
            pending = total - tested

            # Progress bar
            pct    = (tested / total * 100) if total else 0
            filled = int(pct / 2)
            bar    = "█" * filled + "░" * (50 - filled)
            self.query_one("#pc-bar", Static).update(
                f"[cyan]{bar[:40]}[/][dim]{bar[40:]}[/]  [dim]{tested}/{total}[/]"
            )

            # Summary counts
            done_tag = "  [bold green]✓ DONE[/]" if self._done else ""
            self.query_one("#pc-counts", Static).update(
                f"[green]✓ Working: {working}[/]   "
                f"[red]✗ Dead: {dead}[/]   "
                f"[dim]⏳ Pending: {pending}[/]"
                + done_tag
            )

            # Flush new log entries
            log = self.query_one("#pc-log", RichLog)
            while self._log_cursor < len(self._results):
                url, ok, ms, err = self._results[self._log_cursor]
                label = url or "direct"
                if ok:
                    log.write(
                        f"[green]✓[/]  [bold white]{label:<42}[/]  "
                        f"[green]{ms}ms[/]"
                    )
                else:
                    log.write(
                        f"[red]✗[/]  [dim]{label:<42}[/]  "
                        f"[red]{err}[/]"
                    )
                self._log_cursor += 1

            # Update buttons
            btn = self.query_one("#btn-proceed", Button)
            btn.label    = f"▶  Start with {working} working proxies"
            btn.disabled = working == 0

            save_btn = self.query_one("#btn-save", Button)
            save_btn.disabled = not (self._done and working > 0)

            # Fire once when testing finishes
            if self._done and not self._notified_done:
                self._notified_done = True
                self.app.bell()
                self.app.notify(
                    f"{working} working / {dead} dead — click Save to write them to a file",
                    title="Proxy check done",
                    severity="information",
                    timeout=8,
                )

        except Exception:
            pass

    @on(Button.Pressed, "#btn-proceed")
    def _proceed(self) -> None:
        working = [url for url, ok, _, _ in self._results if ok]
        if not working:
            return
        if self._test_task:
            self._test_task.cancel()
        self.app.push_screen(
            CheckerScreen(self.wordlist_path, proxy_urls=working, **self.config)
        )

    @on(Button.Pressed, "#btn-save")
    def _save_working(self) -> None:
        working = [url for url, ok, _, _ in self._results if ok]
        if not working:
            return
        out = Path("working_proxies.txt")
        out.write_text("\n".join(working) + "\n", encoding="utf-8")
        self.app.notify(
            f"Saved {len(working)} proxies → {out}",
            title="Saved",
            severity="information",
            timeout=5,
        )

    @on(Button.Pressed, "#btn-pc-back")
    def _back(self) -> None:
        self.action_go_back()

    def action_go_back(self) -> None:
        if self._test_task:
            self._test_task.cancel()
        self.app.pop_screen()


# ══════════════════════════════════════════════════════════════════════════════
# Screen 3 — Checker
# ══════════════════════════════════════════════════════════════════════════════

class CheckerScreen(Screen):
    BINDINGS = [
        Binding("q",      "stop_and_back", "Stop"),
        Binding("escape", "stop_and_back", "Stop"),
    ]

    DEFAULT_CSS = """
    CheckerScreen {
        background: #090912;
    }
    #top-row {
        height: 18;
    }
    #stats-panel {
        width: 40;
        border: round magenta;
        padding: 1 2;
        background: #090912;
    }
    #progress-panel {
        width: 1fr;
        border: round magenta;
        padding: 1 2;
        background: #090912;
    }
    #feed-outer {
        height: 1fr;
        border: round magenta;
        background: #090912;
    }
    #feed-title {
        color: white;
        text-style: bold;
        padding: 0 2;
        height: 1;
    }
    RichLog {
        height: 1fr;
        padding: 0 1;
        background: #090912;
    }
    #checker-footer {
        height: 1;
        color: #2a2a3a;
        text-align: center;
    }
    """

    def __init__(
        self,
        wordlist_path: Path,
        rps: float            = 2.0,
        output: str           = "hits.txt",
        proxy_file: Optional[str]  = None,
        proxy_urls: Optional[list] = None,   # pre-tested list from ProxyCheckerScreen
        min_len: Optional[int]     = None,
        max_len: Optional[int]     = None,
        resume: bool          = False,
        append: bool          = False,
        coordinate: bool      = False,
        webhook_url: Optional[str] = None,
        webhook_max_len: int  = 3,
    ):
        super().__init__()
        self.wordlist_path   = wordlist_path
        self.rps             = rps
        self.output          = output
        self.proxy_file      = proxy_file
        self.proxy_urls      = proxy_urls
        self.min_len         = min_len
        self.max_len         = max_len
        self.resume          = resume
        self.append          = append
        self.coordinate      = coordinate
        self.webhook_url     = webhook_url
        self.webhook_max_len = webhook_max_len

        # Runtime state
        self.stats             = Stats()
        self.feed: list        = []
        self.checked: set      = set()
        self._total            = 0          # codes assigned to this instance
        self._queue: Optional[asyncio.Queue] = None
        self._worker_tasks: list = []
        self._checker_task: Optional[asyncio.Task] = None
        self._out_file         = None
        self._done             = False
        self._notified_done    = False
        self._feed_cursor      = 0

        # Proxy + coordinator info (populated during run)
        self._proxy_urls: list = []
        self._limiters:  list  = []
        self._coord: Optional[WordlistCoordinator] = None
        self._coord_stats: dict = {}

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-row"):
            with Container(id="stats-panel"):
                yield Static("", id="stats-display")
            with Container(id="progress-panel"):
                yield Static("", id="progress-display")
        with Container(id="feed-outer"):
            yield Static("  Live Feed", id="feed-title")
            yield RichLog(id="live-log", markup=True, highlight=False,
                          wrap=False, auto_scroll=True)
        yield Static(
            "Q / Escape — stop & back    Ctrl+C — force quit",
            id="checker-footer",
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self._checker_task = asyncio.create_task(self._run_checker())
        self.set_interval(0.15, self._refresh_ui)

    # ── Main checker coroutine ────────────────────────────────────────────────

    async def _run_checker(self) -> None:
        # 1. Load & filter wordlist
        codes = [
            line.strip()
            for line in self.wordlist_path.read_text(
                encoding="utf-8", errors="ignore"
            ).splitlines()
            if line.strip()
        ]
        if self.min_len is not None:
            codes = [c for c in codes if len(c) >= self.min_len]
        if self.max_len is not None:
            codes = [c for c in codes if len(c) <= self.max_len]

        # 2. Resolve proxy list  (pre-tested list takes priority over file)
        if self.proxy_urls is not None:
            proxies = self.proxy_urls
        elif self.proxy_file:
            proxies = load_proxies(Path(self.proxy_file))
        else:
            proxies = []
        if not proxies:
            proxies = [None]   # single direct-connection "proxy"
        self._proxy_urls = proxies

        n_proxies = len(proxies)
        n_workers = n_proxies           # 1 worker per proxy — simple and effective
        self._limiters = [RateLimiter(self.rps) for _ in proxies]

        # 3. Setup coordinator (optional)
        if self.coordinate:
            self._coord = WordlistCoordinator(self.wordlist_path, len(codes))
            self._coord.initialize()

        # 4. Output file + checkpoint path
        cp_path = Path(self.output).with_suffix(PROGRESS_EXT) if self.output else None
        if self.resume and cp_path:
            self.checked = load_checkpoint(cp_path)
            if self.checked and not self.coordinate:
                codes = [c for c in codes if c not in self.checked]

        file_mode = "a" if (self.append or self.resume) else "w"
        self._out_file = open(self.output, file_mode, encoding="utf-8") if self.output else None

        # 5. Create one AsyncClient per proxy
        clients = []
        try:
            for url in proxies:
                kw = {"headers": _HEADERS, "follow_redirects": True, "http2": _USE_HTTP2}
                if url:
                    kw["proxy"] = url
                clients.append(httpx.AsyncClient(**kw))

            # 6. Create queue and start workers
            self._queue = asyncio.Queue()
            self._worker_tasks = [
                asyncio.create_task(
                    _checker_worker(
                        clients[i % n_proxies],
                        self._limiters[i % n_proxies],
                        self._queue, self.stats, self.feed,
                        self._out_file, self.checked, cp_path,
                        webhook_url     = self.webhook_url,
                        webhook_max_len = self.webhook_max_len,
                    )
                )
                for i in range(n_workers)
            ]

            # 7. Feed the queue
            if self.coordinate and self._coord:
                await self._coordinated_feed(codes, n_workers)
            else:
                self._total = len(codes)
                for code in codes:
                    await self._queue.put(code)
                for _ in range(n_workers):
                    await self._queue.put(None)   # sentinels
                await asyncio.gather(*self._worker_tasks, return_exceptions=True)

        finally:
            for c in clients:
                await c.aclose()
            if self._out_file:
                self._out_file.close()
                self._out_file = None
            if cp_path:
                save_checkpoint(cp_path, self.checked)
            if self._coord:
                self._coord.release_mine()
            self._done = True

    async def _coordinated_feed(self, codes: list, n_workers: int) -> None:
        """Claim chunks one at a time, fill queue, wait for each to drain, repeat."""
        coord = self._coord
        while True:
            chunk = coord.claim_next()
            if chunk is None:
                break                  # no more free chunks for this instance
            start, end = chunk
            chunk_codes = codes[start:end]
            self._total += len(chunk_codes)

            for code in chunk_codes:
                await self._queue.put(code)

            await self._queue.join()   # wait for this chunk to be fully processed
            coord.mark_done(start, end)

            # Refresh coordinator global stats for the UI
            self._coord_stats = coord.global_stats()

        # All chunks claimed — send sentinels to stop workers
        for _ in range(n_workers):
            await self._queue.put(None)
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)

    # ── UI refresh (delta-only — no flicker) ──────────────────────────────────

    def _refresh_ui(self) -> None:
        try:
            self._update_stats()
            self._update_progress()
            self._flush_feed()
            self._check_notifications()
        except Exception:
            pass

    def _check_notifications(self) -> None:
        s = self.stats
        direct = len(self._proxy_urls) == 1 and self._proxy_urls[0] is None

        # Wordlist finished
        if self._done and not self._notified_done:
            self._notified_done = True
            self.app.bell()
            self.app.notify(
                f"All done — {s.hits} hit{'s' if s.hits != 1 else ''}, "
                f"{s.taken} taken, {s.skipped_rl} RL-skipped",
                title="Wordlist complete",
                severity="information",
                timeout=0,   # stays until dismissed
            )

        # Direct connection approaching rate limit cap (~500-600 req)
        if direct and not self._done and s.requests in range(450, 455):
            self.app.notify(
                "Approaching Discord's per-IP limit (~500 req). "
                "Expect heavy rate limiting soon — consider stopping and resuming later.",
                title="⚠  Rate limit warning",
                severity="warning",
                timeout=12,
            )

    def _update_stats(self) -> None:
        s = self.stats
        n_proxies  = len([u for u in self._proxy_urls if u is not None])
        direct     = len(self._proxy_urls) == 1 and self._proxy_urls[0] is None
        proxy_line = (
            "[dim]Direct [/]  [dim](no proxy)[/]"
            if direct else
            f"[dim]Proxies[/]  [bold white]{n_proxies}[/] [dim]loaded[/]"
        )

        target_rps = self.rps * len(self._proxy_urls) if self._limiters else self.rps
        rps_col = "green" if s.live_rps >= target_rps * 0.75 else "yellow"
        if self._done:
            status = "[bold green]COMPLETE[/]"
        elif s.is_rate_limited:
            status = "[bold red]RATE LIMITED[/] [dim](auto-slowing)[/]"
        else:
            status = "[green]RUNNING[/]"

        wh_line = ""
        if self.webhook_url:
            limit = f"≤{self.webhook_max_len}" if self.webhook_max_len > 0 else "all"
            wh_line = f"\n[dim]Webhook [/]  [green]✓ on[/] [dim]({limit} chars)[/]"

        rl_line = (
            f"[dim]RL skip[/]  [bold red]{s.skipped_rl}[/] [dim](resume later)[/]\n"
            if s.skipped_rl else ""
        )
        self.query_one("#stats-display", Static).update(
            f"[dim]Status  [/]  {status}\n"
            f"{proxy_line}"
            f"{wh_line}\n"
            f"\n"
            f"[dim]Requests[/]  [bold white]{s.requests:,}[/]\n"
            f"[dim]RPS     [/]  [{rps_col}]{s.live_rps}[/{rps_col}]"
            f"[dim]  (peak {s.peak_rps})[/]\n"
            f"\n"
            f"[dim]Hits  [/]   [bold green]{s.hits}[/]  🔥\n"
            f"[dim]Taken [/]   [bold red]{s.taken}[/]\n"
            f"[dim]Errors[/]   [bold yellow]{s.errors}[/]\n"
            f"[dim]Retries[/]  [bold magenta]{s.retries}[/]\n"
            f"{rl_line}"
            f"\n"
            f"[dim]Hit-rate[/] [bold cyan]{s.hit_rate:.1f}%[/]"
        )

    def _update_progress(self) -> None:
        s     = self.stats
        total = self._total
        q     = self._queue
        done  = total - (q.qsize() if q is not None else total)
        done  = max(0, min(done, total))
        remaining = total - done
        pct   = (done / total * 100) if total else 0.0
        filled = int(pct / 2)
        bar   = "█" * filled + "░" * (50 - filled)

        # Coordinator section (only shown when coordinating)
        coord_line = ""
        if self._coord is not None:
            cs = self._coord_stats
            if cs:
                coord_line = (
                    f"\n[dim]Coord[/]    "
                    f"[green]{cs.get('done', 0)}[/][dim] done  [/]"
                    f"[yellow]{cs.get('claimed', 0)}[/][dim] active  [/]"
                    f"[dim]{cs.get('free', 0)} free[/]"
                    f"[dim]  /  {cs.get('total', 0)} chunks[/]"
                )

        n_proxies = max(1, len(self._proxy_urls))
        proxy_label = (
            f"[dim]{n_proxies} {'proxies' if n_proxies > 1 else 'proxy'}[/]"
            if self._proxy_urls and self._proxy_urls[0] is not None
            else "[dim]direct[/]"
        )
        self.query_one("#progress-display", Static).update(
            f"[dim]Progress[/]\n"
            f"[cyan]{bar[:38]}[/][dim]{bar[38:50]}[/]\n"
            f"[bold white]{pct:.1f}%[/]  "
            f"[dim]{done:,} / {total:,}  ({remaining:,} left)[/]\n"
            f"\n"
            f"[dim]Elapsed [/]  [bold white]{s.elapsed}[/]\n"
            f"[dim]ETA     [/]  [bold white]{s.eta(remaining)}[/]\n"
            f"[dim]Avg RPS [/]  [dim]{s.avg_rps:.1f}[/]\n"
            f"[dim]Workers [/]  {proxy_label}"
            + coord_line
        )

    def _flush_feed(self) -> None:
        """Append only new entries to the log — zero re-renders."""
        if self._feed_cursor >= len(self.feed):
            return
        log = self.query_one("#live-log", RichLog)
        while self._feed_cursor < len(self.feed):
            e    = self.feed[self._feed_cursor]
            ts   = f"[dim]{e.ts}[/]"
            code = f"[bold white]{e.code:<22}[/]"
            if e.status == "HIT":
                log.write(f"{ts}  [bold green]🔥 HIT  [/]  {code}  [green]{e.info}[/]")
            elif e.status == "TAKEN":
                log.write(f"{ts}  [bold red]✗ TAKEN[/]  {code}  [dim]{e.info}[/]")
            else:
                log.write(f"{ts}  [yellow]⚠  ERR [/]  {code}  [dim]{e.info}[/]")
            self._feed_cursor += 1

    # ── Stop ──────────────────────────────────────────────────────────────────

    async def action_stop_and_back(self) -> None:
        if self._checker_task:
            self._checker_task.cancel()
        for t in self._worker_tasks:
            t.cancel()
        await asyncio.gather(
            *(([self._checker_task] if self._checker_task else []) + self._worker_tasks),
            return_exceptions=True,
        )
        if self._out_file:
            self._out_file.close()
            self._out_file = None
        if self._coord:
            self._coord.release_mine()
        self.app.pop_screen()


# ══════════════════════════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════════════════════════

class VanityCheckerApp(App):
    TITLE = "DC-Checker"
    CSS = """
    Screen { background: #090912; }
    Header { background: #14003a; color: magenta; }
    """

    def __init__(self, initial_wordlist: Optional[Path] = None):
        super().__init__()
        self._initial_wordlist = initial_wordlist

    def on_mount(self) -> None:
        if self._initial_wordlist:
            self.push_screen(ConfigScreen(self._initial_wordlist))
        else:
            self.push_screen(FilePickerScreen(sorted(Path.cwd().glob("*.txt"))))


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="DC-Checker — Discord vanity code checker (Textual TUI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Proxy file format (proxies.txt):\n"
            "  http://user:pass@host:port\n"
            "  socks5://host:port\n"
            "  # lines starting with # are ignored\n\n"
            "Multi-instance (zero overlap):\n"
            "  Enable 'Coordinate instances' in the config screen.\n"
            "  Each terminal will claim separate chunks of the wordlist.\n"
        ),
    )
    ap.add_argument("wordlist", nargs="?", default=None,
                    help="Wordlist path (optional — shows file picker if omitted)")
    args = ap.parse_args()

    initial = None
    if args.wordlist:
        p = Path(args.wordlist)
        if not p.exists():
            sys.exit(f"File not found: {p}")
        initial = p

    VanityCheckerApp(initial_wordlist=initial).run()


if __name__ == "__main__":
    main()
