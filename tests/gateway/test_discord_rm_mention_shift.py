"""D5 (fleet-self-audit-261019, issue #256): inbound mention-resolution shift.

Root cause: ``DiscordAdapter._handle_message`` deletes the bot's own ``<@ID>``
token from inbound content (adapter.py ~L6121-6126). In a multi-lane dispatch
like ``<@A> Alice · <@B> Bob`` every lane receives different text with its own
token missing, so prose names pair with the NEXT lane's ID — "one lane right".
Five lanes misread the Phase-3 dispatch this way (worker.md:84 verbatim).

Fix: when the message mentions self AND at least one other user, keep name↔ID
pairing intact by replacing the self token with ``@You`` instead of deleting
it. Single-mention messages keep the historical strip behavior byte-identical.
"""

import datetime as dt
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.Object = lambda *, id: SimpleNamespace(id=id)
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    # Real exception classes — adapter code catches discord.Forbidden/HTTPException/NotFound.
    class _DiscordHTTPException(Exception):
        pass

    class _DiscordForbidden(_DiscordHTTPException):
        pass

    class _DiscordNotFound(_DiscordHTTPException):
        pass

    discord_mod.HTTPException = _DiscordHTTPException
    discord_mod.Forbidden = _DiscordForbidden
    discord_mod.NotFound = _DiscordNotFound
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import discord  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


BOT_ID = 999
OTHER_BOT_ID = 1560491548569895135  # Infrastructure, per live guild 261020


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = DiscordAdapter(config)
    bot_user = SimpleNamespace(id=BOT_ID, bot=True, display_name="Hermes", name="hermes")
    adapter._client = SimpleNamespace(user=bot_user, get_channel=lambda _id: None)
    adapter._ready_event.set()
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    # Auto-thread off: we assert the normalized text at event time, not thread titles.
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    # Text batching off: assert the exact event text at dispatch, not after a batch timer.
    adapter._text_batch_delay_seconds = 0
    return adapter


def _self_user():
    return SimpleNamespace(id=BOT_ID, bot=True, display_name="Hermes", name="hermes")


def _other_user():
    return SimpleNamespace(id=OTHER_BOT_ID, bot=True, display_name="Infrastructure", name="infrastructure")


def make_message(content: str, mentions: list):
    class _Chan:
        id = 555
        parent_id = None
        name = "planner"
        guild = SimpleNamespace(id=777, name="fleet")
        topic = None

        def history(self, **kwargs):
            async def _gen():
                return
                yield  # pragma: no cover

            return _gen()

    channel = _Chan()
    return SimpleNamespace(
        id=111, content=content, reactions=[], attachments=[], reference=None,
        author=SimpleNamespace(id=42, bot=False, display_name="Beau", name="beau"),
        channel=channel, guild=channel.guild,
        created_at=datetime.now(dt.UTC), mentions=list(mentions),
        type=discord.MessageType.default,
    )


def capture_events(adapter, monkeypatch):
    events: list = []

    async def _handle(event):
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", _handle, raising=False)
    return events


@pytest.mark.asyncio
async def test_multi_agent_dispatch_preserves_name_id_pairing(adapter, monkeypatch):
    """RM regression (issue #256): in <@self> Name · <@other> Other, the self token
    must NOT vanish — each lane's prose name must stay paired with its own ID."""
    events = capture_events(adapter, monkeypatch)
    message = make_message(
        f"<@{BOT_ID}> Coder · <@{OTHER_BOT_ID}> Infrastructure — D1 fix",
        mentions=[adapter._client.user, _other_user()],
    )
    assert await adapter._handle_message(message) is True
    assert events, "no platform event fired"
    text = getattr(events[0], "text", None) or events[0].get("text", "")
    # Name↔ID pairing preserved: other lane's ID intact, self marked @You (not deleted).
    assert f"<@{OTHER_BOT_ID}>" in text, f"other-lane ID lost: {text!r}"
    assert "@You" in text, f"self token silently deleted — one-lane-right shift: {text!r}"
    # The raw self ID must not survive in the rendered text.
    assert f"<@{BOT_ID}>" not in text


@pytest.mark.asyncio
async def test_single_mention_strips_self_token_unchanged(adapter, monkeypatch):
    """Single-mention behavior stays byte-identical to pre-fix strip (no other users)."""
    events = capture_events(adapter, monkeypatch)
    message = make_message(
        f"<@{BOT_ID}> status report please", mentions=[adapter._client.user]
    )
    assert await adapter._handle_message(message) is True
    assert events, "no platform event fired"
    text = getattr(events[0], "text", None) or events[0].get("text", "")
    assert text.strip() == "status report please"


@pytest.mark.asyncio
async def test_no_self_mention_preserves_foreign_tokens(adapter, monkeypatch):
    """Thread-style message (mention check waived): other lanes' tokens pass through
    untouched when self is not mentioned."""
    events = capture_events(adapter, monkeypatch)
    # Simulate an already-joined thread: mention gate bypassed (in_bot_thread True).
    monkeypatch.setattr(adapter, "_in_bot_thread", lambda _msg: True, raising=False)
    message = make_message(
        f"note: <@{OTHER_BOT_ID}> owns D1", mentions=[_other_user()]
    )
    assert await adapter._handle_message(message) is True
    assert events, "no platform event fired"
    text = getattr(events[0], "text", None) or events[0].get("text", "")
    assert f"<@{OTHER_BOT_ID}>" in text
