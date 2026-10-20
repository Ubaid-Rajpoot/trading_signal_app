"""Alert delivery: Telegram bot messages and/or a generic JSON webhook.

No third-party SDKs - just HTTP calls that fail softly. Every attempt is logged
to the notifications table so the UI can show whether delivery works.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ...config import HTTP_TIMEOUT, USER_AGENT
from ..store import get_settings, log_notification

_EMOJI = {"BUY": "🟢", "SELL": "🔴", "HOLD LONG": "🔵", "HOLD SHORT": "🟠", "EXIT": "⚪", "WAIT": "⚫"}


def format_signal(signal: dict[str, Any]) -> str:
    plan = signal.get("plan") or {}
    tps = ", ".join(f"{tp:g}" for tp in (plan.get("take_profits") or [])[:3]) or "—"
    emoji = _EMOJI.get(signal.get("action", "WAIT"), "⚪")
    lines = [
        f"{emoji} *{signal['action']}* {signal['symbol']} · {signal['timeframe']}",
        f"Strategy: {signal['strategy']['name']}",
        f"Price: {signal['price']:g}",
    ]
    if plan.get("entry"):
        lines.append(f"Entry {plan['entry']:g} · Stop {plan.get('stop', 0):g}")
        lines.append(f"Targets: {tps}")
    lines.append(f"Confidence: {signal.get('confidence')}%")
    if signal.get("source") == "demo":
        lines.append("_demo data_")
    return "\n".join(lines)


def _send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.post(url, json=payload)
        if resp.status_code == 200:
            return True, "sent"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _send_webhook(url: str, signal: dict[str, Any]) -> tuple[bool, str]:
    body = {
        "event": "signal",
        "symbol": signal["symbol"],
        "timeframe": signal["timeframe"],
        "action": signal["action"],
        "side": signal.get("plan", {}).get("side"),
        "price": signal["price"],
        "confidence": signal.get("confidence"),
        "plan": signal.get("plan"),
        "strategy": signal["strategy"],
        "bar_time": signal["bar_time"],
        "source": signal.get("source"),
        "text": format_signal(signal),
    }
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.post(url, json=body)
        if 200 <= resp.status_code < 300:
            return True, "sent"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


def deliver(signal: dict[str, Any], signal_id: int | None = None) -> list[dict[str, Any]]:
    """Fan a signal out to every configured channel. Returns per-channel results."""
    alerts = get_settings()["alerts"]
    results: list[dict[str, Any]] = []

    if alerts.get("telegram_enabled"):
        token = alerts.get("telegram_bot_token") or ""
        chat_id = alerts.get("telegram_chat_id") or ""
        if token and chat_id:
            ok, detail = _send_telegram(token, chat_id, format_signal(signal))
            status = "sent" if ok else "failed"
            log_notification(signal_id, "telegram", status, detail)
            results.append({"channel": "telegram", "status": status, "detail": detail})

    if alerts.get("webhook_url"):
        ok, detail = _send_webhook(alerts["webhook_url"], signal)
        status = "sent" if ok else "failed"
        log_notification(signal_id, "webhook", status, detail)
        results.append({"channel": "webhook", "status": status, "detail": detail})

    if not results:
        log_notification(signal_id, "none", "skipped", "No alert channel configured")
        results.append({"channel": "none", "status": "skipped", "detail": "No alert channel configured"})
    return results


def test_channel(channel: str) -> dict[str, Any]:
    alerts = get_settings()["alerts"]
    sample = {
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "action": "BUY",
        "price": 68000.0,
        "confidence": 70,
        "strategy": {"id": "test", "name": "Test"},
        "plan": {"side": "LONG", "entry": 68000.0, "stop": 66500.0, "take_profits": [69500.0, 71000.0]},
        "bar_time": 0,
        "source": "test",
    }
    if channel == "telegram":
        token, chat_id = alerts.get("telegram_bot_token", ""), alerts.get("telegram_chat_id", "")
        if not (token and chat_id):
            return {"channel": "telegram", "status": "failed", "detail": "Bot token or chat id missing"}
        ok, detail = _send_telegram(token, chat_id, "✅ SignalForge test alert — delivery works.")
        return {"channel": "telegram", "status": "sent" if ok else "failed", "detail": detail}
    if channel == "webhook":
        url = alerts.get("webhook_url", "")
        if not url:
            return {"channel": "webhook", "status": "failed", "detail": "Webhook URL missing"}
        ok, detail = _send_webhook(url, sample)
        return {"channel": "webhook", "status": "sent" if ok else "failed", "detail": detail}
    return {"channel": channel, "status": "failed", "detail": f"Unknown channel '{channel}'"}
