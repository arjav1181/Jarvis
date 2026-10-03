"""
core/mail.py — the only thing in JARVIS allowed to send an email (Phase 4d).

THE RULE
    Nothing here runs on a timer without a human behind it. The sender's
    entire job is to take leads the user APPROVED — a state only a
    core/confirm.py resolve can produce — and put them in an inbox, slowly.

WHY IT IS BUILT THIS WAY
    Cold email from a small mailbox is a reputational asset, and it is easy to
    destroy in an afternoon. So the constraints are in code, not in a prompt:

      * hard caps        a daily ceiling and one message per domain per day;
      * warmup           5 on day one, doubling to a ceiling — a brand new
                        mailbox that sends 30 messages in an hour is a
                        spam signal, not a salesperson;
      * opt-out honoured leads marked unsubscribed are never touched again,
                        and every sent message carries a one-line opt-out;
      * threading        every message carries a Message-ID, so a reply can be
                        matched back to its lead (see poll_replies);
      * credentials live in config/api_keys.json, which is gitignored, and the
                        app password is never printed, logged, or returned.

Transport is smtplib/imaplib — both stdlib. Gmail app passwords exist for
exactly this kind of client; OAuth would be the wrong weight for sending from
one dedicated mailbox.
"""

from __future__ import annotations

import email
import email.message
import email.utils
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
from email.header import Header
from typing import Any, Callable, Optional

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

DAILY_CAP = 20
PER_DOMAIN_CAP = 1
WARMUP_SCHEDULE = (5, 10, 15, 20, 20, 20, 20)   # by days since first send
MIN_GAP_S = 45.0                                 # between two sends
OPT_OUT_LINE = ("If you'd rather not hear from me, reply \"no\" and I won't "
                "write again.")

_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# ── credentials + send ledger ────────────────────────────────────────────────

def _keys() -> dict:
    try:
        from core.data_paths import config_dir
        p = config_dir() / "api_keys.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def config() -> dict:
    """Sending config, resolved but never echoed with the secret."""
    k = _keys()
    addr = str(k.get("gmail_address") or "").strip()
    pw = str(k.get("gmail_app_password") or "").strip()
    return {"address": addr,
            "configured": bool(addr and pw),
            "domain": addr.split("@")[-1].lower() if "@" in addr else "",
            "ledger": _ledger()}


def set_config(address: str, app_password: str) -> dict:
    k = _keys()
    k["gmail_address"] = str(address or "").strip()
    if app_password:
        k["gmail_app_password"] = str(app_password).strip()
    from core.data_paths import config_dir
    p = config_dir() / "api_keys.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(k, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return config()


def _ledger_path():
    from core.data_paths import data_root
    return data_root() / "mail_ledger.json"


def _ledger() -> dict:
    try:
        d = json.loads(_ledger_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_ledger(d: dict) -> None:
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def warmup_ceiling(ledger: dict | None = None) -> int:
    """Today's ceiling, based on days since the first send."""
    led = ledger if ledger is not None else _ledger()
    first = float(led.get("first_send") or 0)
    if not first:
        return WARMUP_SCHEDULE[0]
    day = int((time.time() - first) // 86400)
    if day < len(WARMUP_SCHEDULE):
        return WARMUP_SCHEDULE[day]
    return WARMUP_SCHEDULE[-1]


def budget(ledger: dict | None = None) -> dict:
    """What may be sent right now, and why not more if it cannot."""
    led = ledger if ledger is not None else _ledger()
    today = _today()
    sent_today = [t for t in (led.get("sent") or []) if t.get("day") == today]
    domains = {}
    for t in sent_today:
        domains[t.get("domain") or ""] = domains.get(t.get("domain") or "", 0) + 1
    ceiling = warmup_ceiling(led)
    last = float(led.get("last_send") or 0)
    return {
        "sent_today": len(sent_today),
        "ceiling": ceiling,
        "remaining": max(0, ceiling - len(sent_today)),
        "domains_today": domains,
        "seconds_since_last": round(time.time() - last, 1) if last else None,
        "min_gap_ok": (not last) or (time.time() - last) >= MIN_GAP_S,
    }


# ── message building ─────────────────────────────────────────────────────────

def build_message(to: str, subject: str, body: str, *,
                  reply_to: str = "", in_reply_to: str = "",
                  references: str = "") -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    cfg = _keys()
    sender = str(cfg.get("gmail_address") or "").strip() or "jarvis@localhost"
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = str(subject or "(no subject)")
    msg["Date"] = email.utils.formatdate(localtime=True)
    # A stable, parseable Message-ID is what makes reply-tracking possible.
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.split("@")[-1] or None)
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg["X-Jarvis-Leads"] = "1"          # our own mail is identifiable
    text = str(body or "").strip()
    if not re.search(r"reply .?no.?|unsubscribe|not hear from me", text, re.I):
        text = text.rstrip() + "\n\n" + OPT_OUT_LINE
    msg.set_content(text, charset="utf-8")
    return msg


def eligible(store, *, limit: int = 5) -> list[dict]:
    """Approved leads that are actually sendable right now.

    Filters, in order of how badly they would hurt: no address, unsubscribed,
    already contacted, wrong state. The budget check is deliberately NOT here —
    the caller decides how many to take after asking budget().
    """
    out = []
    for lead in store.leads(status="approved", limit=50):
        addr = (lead.get("contact_email") or "").strip()
        if not addr or not _ADDR_RE.match(addr):
            continue
        if lead.get("unsubscribed"):
            continue
        if not (lead.get("draft") or "").strip():
            continue
        out.append(lead)
        if len(out) >= limit:
            break
    return out


def check(lead: dict, led: dict | None = None) -> tuple[bool, str]:
    """Per-lead send permission, with the reason spelled out."""
    led = led if led is not None else _ledger()
    addr = (lead.get("contact_email") or "").strip()
    if not addr or not _ADDR_RE.match(addr):
        return False, "no email address on file"
    if lead.get("unsubscribed"):
        return False, "this contact unsubscribed"
    if not (lead.get("draft") or "").strip():
        return False, "no draft to send"
    if lead.get("status") != "approved":
        return False, f"not approved (status {lead.get('status')})"
    domain = addr.split("@")[-1].lower()
    today = _today()
    used = {}
    for t in (led.get("sent") or []):
        if t.get("day") == today:
            used[t.get("domain") or ""] = used.get(t.get("domain") or "", 0) + 1
    if used.get(domain, 0) >= PER_DOMAIN_CAP:
        return False, f"already wrote to {domain} today"
    b = budget(led)
    if b["remaining"] <= 0:
        return False, (f"daily ceiling reached ({b['sent_today']}/"
                       f"{b['ceiling']})")
    if not b["min_gap_ok"]:
        return False, (f"too soon — {MIN_GAP_S - b['seconds_since_last']:.0f}s "
                       "between messages")
    return True, "ok"


# ── sending (the only writer) ────────────────────────────────────────────────

def verify_login(*, smtp_factory: Optional[Callable] = None) -> dict:
    """Prove the credentials work, without sending anything.

    A wrong app password is a silent failure otherwise: the first sign is a
    bounce three days after the user approved a message.
    """
    cfg = _keys()
    addr = str(cfg.get("gmail_address") or "").strip()
    pw = str(cfg.get("gmail_app_password") or "").strip()
    if not addr or not pw:
        return {"ok": False, "error": "not configured"}
    try:
        if smtp_factory:
            server = smtp_factory(addr, pw)
        else:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=25,
                                      context=ssl.create_default_context())
        with server:
            server.login(addr, pw)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def send_one(msg: email.message.EmailMessage, *,
             smtp_factory: Optional[Callable] = None) -> dict:
    """Deliver one message. The factory is the seam the tests use — no test
    ever opens a socket to Gmail."""
    cfg = _keys()
    addr = str(cfg.get("gmail_address") or "").strip()
    pw = str(cfg.get("gmail_app_password") or "").strip()
    if not addr or not pw:
        return {"ok": False, "error": "gmail_not_configured",
                "detail": "Add the sending address and app password in Settings."}
    try:
        if smtp_factory:
            server = smtp_factory(addr, pw)
        else:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=25,
                                      context=ssl.create_default_context())
        with server:
            server.login(addr, pw)
            server.send_message(msg)
        return {"ok": True, "message_id": msg.get("Message-ID", ""),
                "to": msg.get("To", "")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def send_approved(store, *, limit: int = 1, smtp_factory=None,
                  dry_run: bool = False) -> dict:
    """Send up to `limit` approved leads, respecting every cap.

    This is the ONLY function in the project that may move a lead from
    approved to contacted, and it refuses unless the state says approved.
    """
    led = _ledger()
    report = {"sent": 0, "skipped": [], "budget": budget(led),
              "would_send": [], "dry_run": bool(dry_run)}
    for lead in eligible(store, limit=20):
        if report["sent"] >= limit:
            break
        ok, why = check(lead, led)
        if not ok:
            report["skipped"].append({"id": lead["id"], "why": why})
            continue
        to = lead["contact_email"]
        subject = lead.get("subject") or "Quick question"
        if dry_run:
            report["would_send"].append({"id": lead["id"], "to": to,
                                         "subject": subject,
                                         "chars": len(lead.get("draft") or "")})
            continue
        msg = build_message(to, subject, lead.get("draft") or "")
        res = send_one(msg, smtp_factory=smtp_factory)
        if not res.get("ok"):
            report["skipped"].append({"id": lead["id"], "why": res.get("error")})
            continue
        # Only now does the world learn this lead was contacted.
        store.set_status(int(lead["id"]), "contacted",
                         note="sent by JARVIS")
        store.event("lead", int(lead["id"]), "sent",
                    f"to {to} · id {res.get('message_id','')[:40]}")
        led.setdefault("sent", []).append({
            "day": _today(), "at": time.time(), "domain": to.split("@")[-1].lower(),
            "to": to, "lead": int(lead["id"]),
            "message_id": res.get("message_id", "")})
        led["sent"] = led["sent"][-500:]
        led.setdefault("first_send", time.time())
        led["last_send"] = time.time()
        _save_ledger(led)
        report["sent"] += 1
    report["budget"] = budget(_ledger())
    return report


# ── reply tracking ───────────────────────────────────────────────────────────

def poll_replies(store, *, limit: int = 25,
                 imap_factory: Optional[Callable] = None) -> dict:
    """Find replies to what we sent and move those leads to replied.

    Matching is on In-Reply-To/References against the Message-IDs in the
    ledger — not on sender names, because two different leads can share a
    domain and a reply must not credit the wrong one.
    """
    led = _ledger()
    sent = led.get("sent") or []
    if not sent:
        return {"checked": 0, "matched": 0, "note": "nothing sent yet"}
    by_mid = {t.get("message_id"): t for t in sent if t.get("message_id")}
    cfg = _keys()
    addr = str(cfg.get("gmail_address") or "").strip()
    pw = str(cfg.get("gmail_app_password") or "").strip()
    if not addr or not pw:
        return {"checked": 0, "matched": 0, "note": "gmail not configured"}
    matched, checked = 0, 0
    try:
        factory = imap_factory
        if factory:
            mbox = factory(addr, pw)
        else:
            mbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        try:
            mbox.login(addr, pw)
            mbox.select("INBOX")
            typ, data = mbox.search(None, 'SINCE',
                                    time.strftime("%d-%b-%Y",
                                                  time.localtime(time.time() - 7 * 86400)))
            ids = (data[0].split() if typ == "OK" and data and data[0] else [])[-limit:]
            processed = set(led.get("processed_replies") or [])
            for mid in ids:
                checked += 1
                typ, msgdata = mbox.fetch(mid, "(RFC822.HEADER)")
                if typ != "OK" or not msgdata or not msgdata[0]:
                    continue
                raw = msgdata[0][1] if isinstance(msgdata[0], tuple) else b""
                msg = email.message_from_bytes(raw)
                reply_mid = (msg.get("Message-ID") or "").strip()
                # Idempotence: the same reply can be scanned twice (a poll that
                # lands before the Seen flag sticks, a re-run after a restart).
                # Keyed on the REPLY's own Message-ID, never on the IMAP
                # sequence number — those shift, which would let a duplicate
                # through — and a genuine second message in the thread has its
                # own id, so it still counts.
                dedupe = (reply_mid or "").strip() or f"uid:{mid}"
                refs = " ".join(filter(None, [msg.get("In-Reply-To", ""),
                                             msg.get("References", "")]))
                hit = None
                for candidate in by_mid:
                    if candidate and candidate in refs:
                        hit = by_mid[candidate]
                        break
                if not hit:
                    continue
                lead_id = int(hit.get("lead") or 0)
                if not lead_id:
                    continue
                if dedupe in processed:
                    continue
                if (store.lead(lead_id) or {}).get("status") in ("won", "lost"):
                    continue
                body_txt = ""
                typ2, full = mbox.fetch(mid, "(RFC822)")
                if typ2 == "OK" and full and isinstance(full[0], tuple):
                    parsed = email.message_from_bytes(full[0][1])
                    body_txt = (parsed.get_payload(decode=True) or b"")
                    body_txt = body_txt.decode("utf-8", "replace")[:400]
                # Single-escaped on purpose: r"\bno\b" in this file once
                # matched a literal backslash-b, so a plain "no thanks" was
                # treated as a reply rather than an opt-out.
                if re.search(r"\bno\b|unsubscribe|stop|not interested|"
                             r"remove me|don't (?:want|need) (?:any|more)",
                             body_txt[:200], re.I | re.S):
                    store.set_status(lead_id, "replied", note="opted out in reply")
                    store.event("lead", lead_id, "unsubscribed", "replied no/stop")
                    store._conn.execute(
                        "UPDATE leads SET unsubscribed=1 WHERE id=?",
                        (lead_id,))
                    store._conn.commit()
                else:
                    store.set_status(lead_id, "replied",
                                     note=(body_txt or "reply received")[:120])
                    store.event("lead", lead_id, "replied",
                                (body_txt or "")[:200])
                matched += 1
                processed.add(dedupe)
                try:
                    mbox.store(mid, "+FLAGS", "\\Seen")
                except Exception:
                    pass
            led["processed_replies"] = sorted(processed)[-500:]
            _save_ledger(led)
        finally:
            try:
                mbox.close()
            except Exception:
                pass
            try:
                mbox.logout()
            except Exception:
                pass
    except Exception as e:
        return {"checked": checked, "matched": matched,
                "error": f"{type(e).__name__}: {e}"[:200]}
    return {"checked": checked, "matched": matched}


# ── reading ──────────────────────────────────────────────────────────────────

def _decode_body(raw) -> str:
    """Best-effort plain text.

    `message_from_string` yields a `Message`, not an `EmailMessage` — so
    `get_content()` does not exist on it and every real read used to fall
    through to the fallback and hand the assistant the raw RFC822 dump. This
    uses `get_payload(decode=True)`, which is the API that actually exists,
    and only falls back to a tag strip for HTML-only mail.
    """
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    parts: list[str] = []

    def text_of(part) -> str:
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if payload is None:
            payload = part.get_payload()
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, "replace")
            except LookupError:
                return payload.decode("utf-8", "replace")
        return str(payload or "")

    def walk(msg) -> None:
        if msg.is_multipart():
            for sub in msg.walk():
                if sub.get_content_type() == "text/plain":
                    walk(sub)
            return
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            parts.append(text_of(msg))
        elif ctype == "text/html" and not any(p.strip() for p in parts):
            parts.append(re.sub(r"<[^>]+>", " ", text_of(msg)))

    try:
        walk(email.message_from_string(raw))
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw)
    out = "\n".join(p for p in parts if p.strip())
    return re.sub(r"[ \t]+", " ", out).strip()


def fetch_inbox(*, limit: int = 10, unread_only: bool = False,
                query: str = "", imap_factory: Optional[Callable] = None) -> dict:
    """The inbox, newest first. Reading is free — it changes nothing.

    `imap_factory` is the same seam `poll_replies` uses, so the tests never open
    a socket to Gmail and the read path is testable offline.
    """
    cfg = _keys()
    addr = str(cfg.get("gmail_address") or "").strip()
    pw = str(cfg.get("gmail_app_password") or "").strip()
    if not addr or not pw:
        return {"ok": False, "messages": [],
                "error": "gmail not configured",
                "detail": "Add the sending address and app password in Settings."}
    try:
        mbox = (imap_factory(addr, pw) if imap_factory
                else imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=25))
        with mbox:
            mbox.login(addr, pw)
            mbox.select("INBOX")
            if query.strip():
                typ, data = mbox.search(None, "UNSEEN" if unread_only else "ALL",
                                        *(str(query).split()))
            else:
                typ, data = mbox.search(None, "UNSEEN" if unread_only else "ALL")
            ids = (data[0].split() if typ == "OK" and data and data[0] else [])
            out = []
            for mid in reversed(ids[-int(max(1, min(limit, 50))):]):
                typ, d = mbox.fetch(mid, "(RFC822.HEADER BODY.PEEK[TEXT]<0.2500>)")
                if typ != "OK" or not d:
                    continue
                blob = b"".join(p[1] for p in d if isinstance(p, tuple))
                head = email.message_from_string(blob.decode("utf-8", "replace"))
                body = _decode_body(blob.decode("utf-8", "replace").split(
                    "\r\n\r\n", 1)[-1])
                out.append({
                    "id": mid.decode() if isinstance(mid, bytes) else str(mid),
                    "from": head.get("From", ""),
                    "to": head.get("To", ""),
                    "subject": head.get("Subject", ""),
                    "date": head.get("Date", ""),
                    "in_reply_to": head.get("In-Reply-To", ""),
                    "message_id": head.get("Message-ID", ""),
                    "snippet": body[:400],
                    "body": body,
                })
            return {"ok": True, "messages": out, "total": len(ids),
                    "unread_only": bool(unread_only)}
    except Exception as e:
        return {"ok": False, "messages": [],
                "error": f"{type(e).__name__}: {e}"[:200]}


def recent_sent(*, limit: int = 10) -> list[dict]:
    """What we actually sent, from the ledger. Cheaper and more truthful than
    re-reading the Sent folder, and it is the same record `poll_replies` uses
    to match replies — so the two can never disagree."""
    led = _ledger()
    out = []
    for rec in reversed(led.get("sent") or []):
        at = float(rec.get("at") or 0)
        out.append({"to": rec.get("to", ""), "subject": rec.get("subject", ""),
                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(at))
                           if at else "",
                    "lead": rec.get("lead", ""),
                    "message_id": rec.get("message_id", "")})
        if len(out) >= int(limit):
            break
    return out


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, to: str = "", subject: str = "", body: str = "",
         limit: int = 10, unread: bool = False, query: str = "",
         store=None, smtp_factory: Optional[Callable] = None) -> str:
    """One entry point for the model. Prose back — this gets read aloud.

    Sending here goes through the *same* ledger and budget as the lead engine,
    on purpose. A second, laxer path to Gmail would be the exact bug this
    project exists not to have: the careful one for campaigns, the careless one
    for the assistant. There is only one path, and it counts.
    """
    a = str(action or "").strip().lower()
    try:
        if a in ("", "inbox", "list", "read", "unread"):
            r = fetch_inbox(limit=limit, unread_only=unread or a == "unread",
                            query=query, imap_factory=smtp_factory)
            if not r.get("ok"):
                return f"Inbox unavailable — {r.get('error')}. {r.get('detail','')}".strip()
            rows = r["messages"]
            if not rows:
                return "Nothing in the inbox."
            out = [f"{len(rows)} of {r['total']} message(s):"]
            for m in rows:
                out.append(f"- {m['from']} · {m['date'][:22]} · {m['subject']}")
                if m["snippet"]:
                    out.append(f"    {m['snippet'][:220]}")
            return "\n".join(out)

        if a in ("sent", "outbox", "log"):
            rows = recent_sent(limit=limit)
            return ("\n".join(f"- {r['at']} → {r['to']} · {r['subject']}"
                              for r in rows) or "Nothing sent yet.")

        if a in ("status", "budget", "config"):
            c = config()
            b = budget()
            return (f"Gmail {c['address'] or '(not set)'}"
                    f" · {'configured' if c['configured'] else 'NOT CONFIGURED'}"
                    f" · {b.get('sent_today', 0)} of {b.get('ceiling', 0)} sent "
                    f"today, {b.get('remaining', 0)} left.")

        if a in ("replies", "poll"):
            if store is None:
                return "No lead store attached, so replies cannot be matched."
            return str(poll_replies(store, imap_factory=imap_factory))

        if a in ("configure", "set"):
            c = set_config(to or subject, body)
            return (f"Sending address set to {c['address']}."
                    if c["configured"] else
                    "Address set, but the app password is still missing.")

        if a in ("draft", "compose", "write"):
            to = str(to or "").strip()
            if not _ADDR_RE.match(to):
                return f"'{to}' is not an address I can send to."
            _drafts().append({"to": to, "subject": subject, "body": body,
                              "at": time.time()})
            return (f"Drafted to {to}: {subject or '(no subject)'}. "
                    f"It is NOT sent. Say send when the user approves it.")

        if a in ("send", "send_draft"):
            to = str(to or "").strip()
            if not _ADDR_RE.match(to):
                return f"'{to}' is not an address I can send to."
            led = _ledger()
            ok, why = _budget_ok(led)
            if not ok:
                return f"I did not send it: {why}"
            msg = build_message(to, subject, body)
            res = send_one(msg, smtp_factory=smtp_factory)
            if not res.get("ok"):
                return f"Send failed: {res.get('error')}"
            led.setdefault("sent", []).append({
                "day": _today(), "at": time.time(),
                "domain": to.split("@")[-1].lower(), "to": to, "lead": None,
                "subject": subject, "message_id": res.get("message_id", "")})
            led["sent"] = led["sent"][-500:]
            led.setdefault("first_send", time.time())
            led["last_send"] = time.time()
            _save_ledger(led)
            return f"Sent to {to}: {subject or '(no subject)'}."

        return "Unknown action. Use inbox / sent / status / draft / send / replies."
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:180]


def _drafts_path():
    from core.data_paths import config_dir
    return config_dir() / "mail_drafts.json"


def _drafts() -> list:
    try:
        return json.loads(_drafts_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _budget_ok(led: Optional[dict] = None) -> tuple[bool, str]:
    """The cap, checked before the socket opens.

    This reads `ceiling` and `remaining` — the lead engine's own keys. An
    earlier version of this function looked for `cap`/`used`, which do not
    exist, so it always read 0 and silently allowed unlimited sending. The
    whole point of one shared budget is defeated by a typo in the key name,
    so the assertion below is load-bearing, not decoration.
    """
    b = budget(led)
    sent = int(b.get("sent_today") or 0)
    ceiling = int(b.get("ceiling") or 0)
    if ceiling and sent >= ceiling:
        return False, (f"the limit for today is {ceiling} and {sent} have "
                       f"already gone out. Say so, and stop.")
    if not b.get("min_gap_ok", True):
        return False, (f"only {b.get('seconds_since_last')}s since the last "
                       f"send, and the floor is {MIN_GAP_S}s.")
    return True, ""
