#!/usr/bin/env python3
"""CENT@HOME (= CENT@CASA) seat monitor for the CISIA CEnT-S calendar.

Reads the public calendar page and alerts you when
  - a CENT@HOME session has seats available (first release, or seats freed up),
  - a new CENT@HOME date is published,
  - a CENT@HOME session changes to a status this script doesn't recognise.

CENT@UNI sessions are ignored. Standard library only (Python 3.9+), so it runs
on a stock Mac or a free GitHub Actions runner. Alert channels (all free):
ntfy push notifications, Telegram bot, macOS notification + voice.

Usage:
  python3 cents_monitor.py          one check (schedule this every minute)
  python3 cents_monitor.py --show   print what the calendar lists right now
  python3 cents_monitor.py --test   send a test alert through every channel
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent

CALENDAR_URL = "https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese"
BOOKING_URL = "https://testcisia.it/studenti_tolc/login_sso.php"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# CENT@CASA is the Italian name of CENT@HOME: treat them as one format.
FORMAT_ALIASES = {"CENT@CASA": "CENT@HOME"}
WATCHED_FORMAT = "CENT@HOME"

AVAILABLE_TEXTS = {"AVAILABLE SEATS", "POSTI DISPONIBILI"}
AVAILABLE_HINTS = ("registrations are open", "iscrizioni sono aperte")
SOLD_OUT_TEXTS = {"NOT LONGER AVAILABLE", "NO LONGER AVAILABLE", "POSTI ESAURITI"}
SOLD_OUT_HINTS = ("fully booked", "esauriti")

DEFAULTS = {
    "urls": [CALENDAR_URL],
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "mac_notify": True,             # banner + spoken alert + popup (macOS only)
    "mac_open_booking_page": True,  # open the CISIA login page when seats appear
    "reminder_minutes": 10,         # repeat the seats alert while seats stay open...
    "max_reminders": 3,             # ...at most this many times per opening
    "fail_alert_after": 10,         # warn after this many failed checks in a row
    "daily_summary_hour": 9,        # Italy time; null disables the daily "still running" note
    "source_name": "",              # label in alerts; defaults to Mac / GitHub / hostname
}
ENV_OVERRIDES = {
    "NTFY_TOPIC": "ntfy_topic",
    "NTFY_SERVER": "ntfy_server",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    "MONITOR_SOURCE": "source_name",
}

try:
    from zoneinfo import ZoneInfo

    ITALY = ZoneInfo("Europe/Rome")
except Exception:  # no tz database: fall back to UTC rather than crash
    ITALY = dt.timezone.utc

LOG_FILE: Path | None = None


def now_italy() -> dt.datetime:
    return dt.datetime.now(ITALY)


def log(msg: str) -> None:
    line = f"{now_italy():%Y-%m-%d %H:%M:%S} {msg}"
    if LOG_FILE is None:
        print(line, flush=True)
        return
    try:
        # Keep the log small: once it passes 512 KB, keep only the newest half.
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            tail = LOG_FILE.read_bytes()[-256_000:]
            LOG_FILE.write_bytes(tail[tail.find(b"\n") + 1:])
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        print(line, flush=True)


# --------------------------------------------------------------------------- config / state


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    for env, key in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()
    if os.environ.get("DAILY_SUMMARY_HOUR"):
        value = os.environ["DAILY_SUMMARY_HOUR"].strip().lower()
        cfg["daily_summary_hour"] = None if value in ("off", "none", "no") else int(value)
    if not cfg["source_name"]:
        if os.environ.get("GITHUB_ACTIONS"):
            cfg["source_name"] = "GitHub"
        elif sys.platform == "darwin":
            cfg["source_name"] = "Mac"
        else:
            cfg["source_name"] = socket.gethostname()
    return cfg


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- fetch / parse


class CalendarParser(HTMLParser):
    """Collects cell text (and the status tooltip) of every row in <table id="calendario">."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found_table = False
        self.rows: list[list[dict]] = []
        self._in_table = False
        self._nested = 0
        self._row: list[dict] | None = None
        self._cell: list[str] | None = None
        self._title = ""

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append({"text": " ".join("".join(self._cell).split()), "title": self._title})
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            if self._in_table:
                self._nested += 1
            elif attrs.get("id") == "calendario":
                self._in_table = self.found_table = True
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._close_row()
            self._row = []
        elif tag == "td" and self._row is not None:
            self._close_cell()
            self._cell, self._title = [], ""
        elif self._cell is not None and attrs.get("title") and not self._title:
            self._title = attrs["title"]

    def handle_endtag(self, tag):
        if not self._in_table:
            return
        if tag == "table":
            if self._nested:
                self._nested -= 1
            else:
                self._close_row()
                self._in_table = False
        elif tag == "td":
            self._close_cell()
        elif tag == "tr":
            self._close_row()

    def handle_data(self, data):
        if self._in_table and self._cell is not None:
            self._cell.append(data)


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9,it;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as e:  # retry once on a transient error
            last_error = e
            if attempt == 0:
                time.sleep(3)
    raise RuntimeError(f"could not load {url}: {last_error}")


def classify(session: dict) -> str:
    text, hint = session["status"], session["status_hint"].lower()
    if text in SOLD_OUT_TEXTS or any(h in hint for h in SOLD_OUT_HINTS):
        return "sold_out"
    if text in AVAILABLE_TEXTS or any(h in hint for h in AVAILABLE_HINTS):
        return "available"
    if session["seats"].isdigit() and int(session["seats"]) > 0:
        return "available"
    return "other"


def parse_calendar(page: str, url: str) -> list[dict]:
    parser = CalendarParser()
    parser.feed(page)
    parser.close()
    if not parser.found_table:
        raise RuntimeError("calendar table not found (page layout changed, or the request was blocked)")
    data_rows = [r for r in parser.rows if len(r) >= 8]
    if parser.rows and not data_rows:
        raise RuntimeError("calendar rows have an unexpected layout (fewer than 8 columns)")

    language = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("lingua", [""])[0]
    sessions = []
    for cells in data_rows:
        fmt, uni, region, city, deadline, seats, status, date = cells[:8]
        name = fmt["text"].upper()
        s = {
            "format": FORMAT_ALIASES.get(name, name),
            "university": uni["text"],
            "region": region["text"],
            "city": city["text"],
            "deadline": deadline["text"],
            "seats": seats["text"],
            "status": status["text"].upper(),
            "status_hint": status["title"],
            "date": date["text"],
            "language": language,
        }
        s["state"] = classify(s)
        s["key"] = "|".join([language, s["format"], s["university"], s["city"], s["date"]])
        sessions.append(s)
    return sessions


def describe(s: dict, with_status: bool = True) -> str:
    line = f"- Test day {s['date']}: {s['university']} ({s['city'].title()})"
    details = []
    if s["seats"] and s["seats"] != "---":
        details.append(f"seats {s['seats']}")
    details.append(f"book by {s['deadline']} 14:00 Italy time")
    if with_status:
        details.append(s["status"].lower())
    return line + "\n  " + ", ".join(details)


# --------------------------------------------------------------------------- alerts


class Notifier:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    def channels(self, priority: int = 5) -> list[str]:
        out = []
        if self.cfg["ntfy_topic"]:
            out.append("ntfy")
        if self.cfg["telegram_bot_token"] and self.cfg["telegram_chat_id"]:
            out.append("telegram")
        if self.cfg["mac_notify"] and sys.platform == "darwin":
            out.append("mac")
        return out

    def send(self, title: str, body: str, priority: int, tags: list[str],
             speak: str = "", open_booking: bool = False) -> bool:
        """priority: 5 urgent, 4 high, 3 normal, 2 quiet. True if at least one channel delivered."""
        body = f"{body}\n\n[{self.cfg['source_name']}] {now_italy():%d/%m %H:%M} Italy time"
        delivered = False
        channels = self.channels(priority)
        for name in channels:
            try:
                getattr(self, "_" + name)(title, body, priority, tags, speak, open_booking)
                delivered = True
            except Exception as e:
                log(f"alert via {name} failed: {e}")
        if not channels:
            log("no alert channel configured: set ntfy_topic or Telegram in config.json")
        log(f"alert sent ({'ok' if delivered else 'FAILED'}): {title}")
        return delivered

    @staticmethod
    def _post_json(url: str, payload: dict) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "cents-monitor"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()

    def _ntfy(self, title, body, priority, tags, speak, open_booking):
        payload = {
            "topic": self.cfg["ntfy_topic"],
            "title": title,
            "message": body,
            "priority": priority,
            "tags": tags,
        }
        if priority >= 4:
            payload["click"] = BOOKING_URL
            payload["actions"] = [{"action": "view", "label": "Book now", "url": BOOKING_URL}]
        self._post_json(self.cfg["ntfy_server"].rstrip("/"), payload)

    def _telegram(self, title, body, priority, tags, speak, open_booking):
        url = f"https://api.telegram.org/bot{self.cfg['telegram_bot_token']}/sendMessage"
        text = f"{title}\n\n{body}"
        if priority >= 4:
            text += f"\n\nBook: {BOOKING_URL}"
        self._post_json(url, {
            "chat_id": self.cfg["telegram_chat_id"],
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": priority <= 2,
        })

    def _mac(self, title, body, priority, tags, speak, open_booking):
        def q(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        first_line = body.strip().splitlines()[0]
        sound = "Sosumi" if priority >= 5 else "Glass"
        subprocess.run(
            ["osascript", "-e", f'display notification "{q(first_line)}" with title "{q(title)}" sound name "{sound}"'],
            check=True, timeout=15, capture_output=True,
        )
        if speak:
            subprocess.Popen(["say", speak])
        if priority >= 5:
            # A popup that stays on screen until dismissed (banners disappear).
            subprocess.Popen(["osascript", "-e", f'display alert "{q(title)}" message "{q(body)}" as critical'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if open_booking and self.cfg["mac_open_booking_page"]:
            subprocess.Popen(["open", BOOKING_URL])


# --------------------------------------------------------------------------- main check


def minutes_since(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (now_italy() - dt.datetime.fromisoformat(iso)).total_seconds() / 60
    except ValueError:
        return float("inf")


def run_check(cfg: dict, state_path: Path, html_file: Path | None = None) -> None:
    state = load_state(state_path)
    first_run = "sessions" not in state
    notifier = Notifier(cfg)
    now = now_italy()
    stamp = now.isoformat(timespec="seconds")

    # 1. Read the calendar. Failures are counted so a broken monitor never stays silent.
    try:
        sessions = []
        for url in cfg["urls"]:
            page = html_file.read_text(encoding="utf-8") if html_file else fetch(url)
            sessions += parse_calendar(page, url)
    except Exception as e:
        fails = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = fails
        state["last_error"] = str(e)[:300]
        log(f"FAIL ({fails} in a row): {e}")
        if fails == cfg["fail_alert_after"]:
            notifier.send(
                "CENT monitor can't read the calendar",
                f"{fails} checks in a row failed, so you will NOT get seat alerts until this is fixed.\n"
                f"Last error: {e}\nCheck the calendar yourself meanwhile: {CALENDAR_URL}",
                priority=4, tags=["warning"],
            )
        save_state(state_path, state)
        return

    if state.get("consecutive_failures", 0) >= cfg["fail_alert_after"]:
        notifier.send("CENT monitor is working again",
                      f"The calendar is readable again after {state['consecutive_failures']} failed checks.",
                      priority=2, tags=["white_check_mark"])
    state["consecutive_failures"] = 0
    state.pop("last_error", None)

    # 2. Compare CENT@HOME sessions with what we saw last time.
    watched = [s for s in sessions if s["format"] == WATCHED_FORMAT]
    old = state.get("sessions", {})
    new_sessions: dict = {}
    opened, reminders, released, changed = [], [], [], []

    for s in watched:
        prev = old.get(s["key"])
        rec = {
            "date": s["date"], "university": s["university"], "status": s["status"],
            "seats": s["seats"], "state": s["state"],
            "first_seen": prev["first_seen"] if prev else stamp,
            "alerted_at": prev.get("alerted_at") if prev else None,
            "reminders": prev.get("reminders", 0) if prev else 0,
        }
        if s["state"] == "available":
            if prev is None or prev.get("state") != "available":
                opened.append(s)
                rec["alerted_at"], rec["reminders"] = stamp, 0
            elif rec["reminders"] < cfg["max_reminders"] and minutes_since(rec["alerted_at"]) >= cfg["reminder_minutes"]:
                reminders.append(s)
                rec["alerted_at"], rec["reminders"] = stamp, rec["reminders"] + 1
        elif prev is None:
            if not first_run:
                released.append(s)
        elif s["state"] == "other" and s["status"] != prev.get("status"):
            changed.append((s, prev.get("status", "?")))
        new_sessions[s["key"]] = rec

    # 3. Send alerts. If nothing could be delivered, keep the old state so the next run retries.
    delivered = True
    if first_run:
        available = sum(1 for s in watched if s["state"] == "available")
        listing = "\n".join(describe(s) for s in watched[:15]) or "(none listed right now)"
        delivered &= notifier.send(
            "CENT@HOME monitor started",
            f"Watching the CEnT-S calendar for CENT@HOME / CENT@CASA.\n"
            f"Right now: {len(watched)} CENT@HOME sessions listed, {available} with seats.\n"
            f"You'll get an urgent alert the moment seats open or new dates appear.\n\n{listing}",
            priority=3, tags=["eyes"],
        )
        state["last_summary"] = now.date().isoformat()
    if opened:
        n = len(opened)
        delivered &= notifier.send(
            "CENT@HOME SEATS AVAILABLE - BOOK NOW" if n == 1 else f"{n} CENT@HOME SESSIONS HAVE SEATS - BOOK NOW",
            "\n".join(describe(s, with_status=False) for s in opened) + f"\n\nBook in your CISIA area: {BOOKING_URL}",
            priority=5, tags=["rotating_light"],
            speak="CENT at home seats are available. Book now.", open_booking=True,
        )
    if reminders:
        delivered &= notifier.send(
            "Reminder: CENT@HOME seats still available",
            "\n".join(describe(s, with_status=False) for s in reminders) + f"\n\nBook: {BOOKING_URL}",
            priority=5, tags=["alarm_clock"], speak="Reminder. CENT at home seats are still available.",
        )
    if released:
        delivered &= notifier.send(
            "New CENT@HOME date published",
            "\n".join(describe(s) for s in released)
            + "\n\nNo seats bookable yet. You'll get an urgent alert when booking opens.",
            priority=4, tags=["calendar"], speak="New CENT at home date published.",
        )
    if changed:
        delivered &= notifier.send(
            "CENT@HOME status changed",
            "\n".join(f"{describe(s, with_status=False)}\n  status: {before.lower()} -> {s['status'].lower()}"
                      for s, before in changed)
            + f"\n\nCheck: {CALENDAR_URL}",
            priority=4, tags=["calendar"],
        )

    hour = cfg.get("daily_summary_hour")
    today = now.date().isoformat()
    if hour is not None and not first_run and now.hour >= int(hour) and state.get("last_summary") != today:
        bookable = [s for s in watched if s["state"] == "available"]
        if notifier.send(
            "CENT@HOME monitor: still running",
            f"{len(watched)} CENT@HOME sessions listed, {len(bookable)} with seats. "
            f"Checking continuously; you'll get an urgent alert when seats open.",
            priority=2, tags=["white_check_mark"],
        ):
            state["last_summary"] = today

    if delivered:
        state["sessions"] = new_sessions
    state["last_success"] = stamp
    save_state(state_path, state)

    bookable = sum(1 for s in watched if s["state"] == "available")
    log(f"OK {len(watched)} CENT@HOME sessions ({bookable} with seats); alerts: "
        f"{len(opened)} opened, {len(reminders)} reminders, {len(released)} new, {len(changed)} changed")


def main() -> int:
    global LOG_FILE
    ap = argparse.ArgumentParser(description="CENT@HOME / CENT@CASA seat monitor")
    ap.add_argument("--config", type=Path, default=HERE / "config.json")
    ap.add_argument("--state", type=Path, default=HERE / "state.json")
    ap.add_argument("--log", type=Path, help="append log lines to this file instead of stdout")
    ap.add_argument("--html", type=Path, help="parse this saved page instead of downloading (testing)")
    ap.add_argument("--show", action="store_true", help="print the calendar and exit")
    ap.add_argument("--test", action="store_true", help="send a test alert and exit")
    args = ap.parse_args()
    LOG_FILE = args.log
    cfg = load_config(args.config)

    if args.test:
        ok = Notifier(cfg).send(
            "Test: CENT@HOME monitor alerts work",
            "If you can read this, alerts reach you. A real alert looks like this but says BOOK NOW.",
            priority=4, tags=["test_tube"], speak="Test alert from the CENT monitor.",
        )
        return 0 if ok else 1

    if args.show:
        for url in cfg["urls"]:
            page = args.html.read_text(encoding="utf-8") if args.html else fetch(url)
            sessions = parse_calendar(page, url)
            print(f"{url}\n{len(sessions)} sessions on the calendar:")
            for s in sessions:
                mark = ">>" if s["format"] == WATCHED_FORMAT else "  "
                print(f"{mark} {s['format']:<10} {s['date']}  {s['university'][:42]:<42} "
                      f"seats {s['seats']:>4}  {s['state']:<9} {s['status']}")
        print("\n>> = CENT@HOME / CENT@CASA (watched)")
        return 0

    run_check(cfg, args.state, args.html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
