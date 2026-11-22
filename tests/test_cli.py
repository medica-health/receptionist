import asyncio
import sys

import pytest

from receptionist import cli
from receptionist.session import Speech


class FakeSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.heard = []

    async def respond(self, text, stt_lang=None):
        self.heard.append(text)
        for speech in self.replies.pop(0):
            yield speech


async def run_lines(session, lines):
    queue = list(lines)
    out = []

    async def read_line():
        return queue.pop(0) if queue else None

    await cli.run_chat(session, read_line, out.append)
    return "".join(out)


async def test_chat_prints_speech_markers():
    session = FakeSession(
        [
            [Speech("filler", "One moment, let me check."), Speech("token", "Hi "),
             Speech("token", "there.")],
            [Speech("switch_language", lang="hi"), Speech("token", "नमस्ते।"), Speech("end")],
        ]
    )  # fmt: skip
    text = await run_lines(session, ["hello\n", "\n", "namaste\n", "never read\n"])
    assert session.heard == ["hello\n", "namaste\n"]
    assert "receptionist> [One moment, let me check.] Hi there.\n" in text
    assert "[switch → hi] नमस्ते। [end]\n" in text


async def test_chat_stops_at_eof():
    text = await run_lines(FakeSession([]), [])
    assert text == "you> \n"


def test_chat_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--from" in out and "--call-sid" in out


def test_chat_requires_from():
    with pytest.raises(SystemExit) as exc:
        cli.main(["chat"])
    assert exc.value.code == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows event loop policy")
def test_windows_uses_a_selector_loop():
    assert cli.loop_factory() is asyncio.SelectorEventLoop
    assert cli.UVICORN_LOOP == "asyncio:SelectorEventLoop"
    loop = cli.loop_factory()()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
