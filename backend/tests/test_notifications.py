"""One paper trade per cycle, and one alert listing EVERY edge, by Telegram. Nothing here may send a real order."""
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATABASE_PATH"] = ":memory:"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402

call_command("migrate", verbosity=0, interactive=False)

from kalshi_api import algorithm, autotrader, client, forecast, notifier  # noqa: E402
from kalshi_api.models import AutoTradeRun, PaperTrade  # noqa: E402
from kalshi_api.paper import get_account  # noqa: E402


def leg(ticker="KXHIGHMIA-26OCT29-B87.5", side="yes", price=40, model=0.5, kalshi=0.4, label="87° to 88°"):
    return {"ticker": ticker, "label": label, "side": side, "price_cents": price,
            "model_prob": model, "kalshi_prob": kalshi}


# ── Which trades ──────────────────────────────────────────────────────────────

def test_pick_leg_prefers_yes():
    rec = {"yes_trade": leg(side="yes"), "no_trades": [leg(side="no", model=0.05, kalshi=0.5)]}
    assert autotrader.pick_leg(rec)["side"] == "yes"


def test_pick_leg_takes_biggest_no_edge_when_no_yes():
    small = leg(ticker="A", side="no", model=0.30, kalshi=0.35)
    big = leg(ticker="B", side="no", model=0.05, kalshi=0.30)
    assert autotrader.pick_leg({"yes_trade": None, "no_trades": [small, big]})["ticker"] == "B"


def test_pick_leg_none_without_edge():
    assert autotrader.pick_leg({"yes_trade": None, "no_trades": []}) is None


def test_all_legs_lists_yes_first_then_nos_by_edge():
    yes = leg(ticker="Y", side="yes")
    small = leg(ticker="A", side="no", model=0.30, kalshi=0.35)
    big = leg(ticker="B", side="no", model=0.05, kalshi=0.30)
    assert [x["ticker"] for x in autotrader.all_legs({"yes_trade": yes, "no_trades": [small, big]})] == ["Y", "B", "A"]
    assert [x["ticker"] for x in autotrader.all_legs({"yes_trade": None, "no_trades": [small, big]})] == ["B", "A"]
    assert autotrader.all_legs({"yes_trade": None, "no_trades": []}) == []


# ── The message ───────────────────────────────────────────────────────────────

def test_message_lists_every_edge_and_marks_the_paper_trade():
    legs = [leg(ticker="Y", side="yes", price=40, model=0.50, kalshi=0.40, label="87° to 88°"),
            leg(ticker="B", side="no", price=30, model=0.05, kalshi=0.30, label="89° or above"),
            leg(ticker="A", side="no", price=55, model=0.30, kalshi=0.45, label="83° to 84°")]
    subject, body = notifier.format_edge_alert(legs, "12:30 PM", 88.4, 50, traded=legs[0], paper_note="Paper account: x")
    assert subject == "Kalshi edge — 12:30 PM ET: 3 trades to consider"
    assert "1. BUY YES · 87° to 88°  ★ paper traded" in body
    assert "2. BUY NO · 89° or above" in body and "3. BUY NO · 83° to 84°" in body
    assert body.count("★") == 1
    assert "Model 5% YES vs Kalshi 30% → edge 25 pts" in body and "30¢ per contract · suggested size 50" in body
    assert "Forecast high: 88.4°F" in body and "Paper account: x" in body


def test_single_edge_subject_is_singular():
    subject, _ = notifier.format_edge_alert([leg()], "1:00 PM", None, 50)
    assert subject == "Kalshi edge — 1:00 PM ET: 1 trade to consider"


def test_nothing_is_starred_when_there_was_no_paper_trade():
    _, body = notifier.format_edge_alert([leg()], "1:00 PM", 88.0, 50, traded=None)
    assert "★" not in body


# ── Telegram ──────────────────────────────────────────────────────────────────

def test_not_configured_means_no_network_call(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EMAIL_USERNAME", "EMAIL_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(notifier, "urlopen", lambda *a, **k: pytest.fail("must not call Telegram"))
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("must not send email"))
    assert notifier.notify_all("s", "b") == []
    assert notifier.channels_status() == {"telegram_configured": False, "email_configured": False}


def test_telegram_request_shape(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        seen["url"], seen["body"] = request.full_url, json.loads(request.data)
        return Resp()

    monkeypatch.setattr(notifier, "urlopen", fake_urlopen)
    assert notifier.send_telegram("hello") == (True, "sent")
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert seen["body"]["chat_id"] == "999" and seen["body"]["text"] == "hello"


@pytest.mark.parametrize("configured, sent_as", [
    ("@my_alerts", "@my_alerts"),            # public channel
    ("my_alerts", "@my_alerts"),             # @ forgotten
    ("-1001234567890", "-1001234567890"),    # private channel id
    (" 999 ", "999"),                        # personal chat, stray spaces
])
def test_channel_ids_are_accepted_in_every_form(monkeypatch, configured, sent_as):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", configured)
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr(notifier, "urlopen", lambda request, timeout: seen.update(body=json.loads(request.data)) or Resp())
    assert notifier.send_telegram("x")[0] is True
    assert seen["body"]["chat_id"] == sent_as


@pytest.mark.parametrize("description, expected", [
    ("Bad Request: chat not found", "@username"),
    ("Forbidden: bot is not a member of the channel chat", "ADMIN"),
    ("Bad Request: not enough rights to send text messages to the chat", "ADMIN"),
    ("Unauthorized", "TELEGRAM_BOT_TOKEN"),
])
def test_setup_mistakes_get_a_plain_hint(monkeypatch, description, expected):
    import io
    import urllib.error

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@c")

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                     io.BytesIO(json.dumps({"ok": False, "description": description}).encode()))

    monkeypatch.setattr(notifier, "urlopen", refuse)
    ok, detail = notifier.send_telegram("x")
    assert ok is False and description in detail and expected in detail
    assert "t/sendMessage" not in detail   # the token/URL never appears in the message


def test_a_failing_telegram_never_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(notifier, "urlopen", boom)
    ok, detail = notifier.send_telegram("hi")
    assert ok is False and "failed" in detail


def test_notify_all_sends_subject_and_body_to_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.delenv("EMAIL_USERNAME", raising=False)
    seen = []
    monkeypatch.setattr(notifier, "send_telegram", lambda text: seen.append(text) or (True, "sent"))
    results = notifier.notify_all("Subject", "Body")
    assert results == [{"channel": "telegram", "ok": True, "detail": "sent"}]
    assert seen == ["Subject\n\nBody"]


# ── Email ─────────────────────────────────────────────────────────────────────

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.started_tls, self.login_args, self.sent = host, port, False, None, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture
def email_env(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setenv("EMAIL_USERNAME", "me@gmail.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    for name in ("EMAIL_TO", "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)


def test_email_goes_to_gmail_over_ssl_with_the_app_password(email_env):
    assert notifier.send_email("Subject line", "Body text") == (True, "sent")
    server = FakeSMTP.instances[0]
    assert (server.host, server.port, server.started_tls) == ("smtp.gmail.com", 465, False)
    assert server.login_args == ("me@gmail.com", "abcdefghijklmnop")   # spaces in the app password are dropped
    message = server.sent[0]
    assert message["To"] == "me@gmail.com" and message["Subject"] == "Subject line"
    assert "Body text" in message.get_content()


def test_email_can_go_to_several_recipients_on_the_starttls_port(email_env, monkeypatch):
    monkeypatch.setenv("EMAIL_TO", "a@x.com, b@y.com")
    monkeypatch.setenv("EMAIL_SMTP_PORT", "587")
    assert notifier.send_email("s", "b")[0] is True
    server = FakeSMTP.instances[0]
    assert server.port == 587 and server.started_tls is True
    assert server.sent[0]["To"] == "a@x.com, b@y.com"


def test_blocked_or_unreachable_smtp_explains_itself_and_never_raises(email_env, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", boom)
    ok, detail = notifier.send_email("s", "b")
    assert ok is False and "could not connect" in detail and "block outgoing SMTP" in detail


def test_wrong_gmail_password_gives_a_helpful_message(email_env, monkeypatch):
    def refuse(*args, **kwargs):
        raise notifier.smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", refuse)
    ok, detail = notifier.send_email("s", "b")
    assert ok is False and "App Password" in detail


def test_a_refused_recipient_is_not_reported_as_a_connection_problem(email_env, monkeypatch):
    def refuse(*args, **kwargs):
        raise notifier.smtplib.SMTPRecipientsRefused({"bad@x.com": (550, b"no such user")})

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", refuse)
    ok, detail = notifier.send_email("s", "b")
    assert ok is False and detail.startswith("Email failed: SMTPRecipientsRefused")


def test_notify_all_reaches_both_channels(email_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(notifier, "send_telegram", lambda text: (True, "sent"))
    results = notifier.notify_all("S", "B")
    assert [(r["channel"], r["ok"]) for r in results] == [("telegram", True), ("email", True)]
    assert FakeSMTP.instances[0].sent[0]["Subject"] == "S"


def test_one_channel_failing_does_not_stop_the_other(email_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(notifier, "send_telegram", lambda text: (False, "Telegram 400: chat not found"))
    results = notifier.notify_all("S", "B")
    assert [(r["channel"], r["ok"]) for r in results] == [("telegram", False), ("email", True)]


# ── The whole cycle ───────────────────────────────────────────────────────────

READY = {"status": "ready", "reason": "Forecast 88.0°F", "mean": 88.0, "yes_trade": None,
         "no_trades": [leg(ticker="A", side="no", price=30, model=0.05, kalshi=0.30, label="89° or above"),
                       leg(ticker="B", side="no", price=40, model=0.25, kalshi=0.40, label="83° to 84°")]}


def _cycle(monkeypatch, rec, auto=True, channel_ok=True):
    AutoTradeRun.objects.all().delete()
    PaperTrade.objects.all().delete()
    account = get_account()
    account.auto_enabled = auto
    account.save()
    sent = []
    monkeypatch.setattr(autotrader, "notify_all",
                        lambda subject, body: sent.append((subject, body)) or
                        [{"channel": "telegram", "ok": channel_ok, "detail": "sent" if channel_ok else "boom"}])
    monkeypatch.setattr(forecast, "get_forecast_cache",
                        lambda: {"last_run_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    monkeypatch.setattr(client, "get_miami_temperature_payload", lambda include_orderbook=False: {"markets": []})
    monkeypatch.setattr(algorithm, "get_full_state", lambda markets: {"recommendation": rec})
    run = autotrader.run_cycle("2026-10-29T12:30")
    return sent, run, PaperTrade.objects.count()


def test_cycle_makes_one_paper_trade_but_alerts_on_every_edge(monkeypatch):
    sent, run, paper = _cycle(monkeypatch, READY)
    assert paper == 1 and len(sent) == 1                       # one paper trade, one message
    subject, body = sent[0]
    assert subject == "Kalshi edge — 12:30 PM ET: 2 trades to consider"
    assert "1. BUY NO · 89° or above  ★ paper traded" in body   # biggest edge first, and it is the paper trade
    assert "2. BUY NO · 83° to 84°" in body and body.count("★ paper traded") == 1   # only one leg is marked
    assert run.status == "traded"


def test_alert_still_goes_out_without_a_paper_trade(monkeypatch):
    # tick() does not start a cycle while Auto is off, but a cycle that does run must still alert.
    sent, run, paper = _cycle(monkeypatch, READY, auto=False)
    assert paper == 0 and len(sent) == 1 and "no paper trade" in sent[0][1] and "★" not in sent[0][1]


def test_no_edge_means_no_alert(monkeypatch):
    waiting = {"status": "waiting", "reason": "No edge", "mean": 88.0, "yes_trade": None, "no_trades": []}
    sent, run, paper = _cycle(monkeypatch, waiting)
    assert sent == [] and paper == 0 and run.status == "skipped"


def test_a_failed_alert_is_noted_but_does_not_stop_the_paper_trade(monkeypatch):
    sent, run, paper = _cycle(monkeypatch, READY, channel_ok=False)
    assert paper == 1 and "telegram alert failed: boom" in run.message and run.status == "traded"


def test_a_slot_alerts_only_once(monkeypatch):
    sent, run, paper = _cycle(monkeypatch, READY)
    assert autotrader.run_cycle("2026-10-29T12:30") is None   # same slot again: nothing happens
    assert len(sent) == 1
