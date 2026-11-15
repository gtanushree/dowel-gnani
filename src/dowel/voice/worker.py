"""The LiveKit worker for the walkthrough. One job per conversation.

Run with `dowel talk`, which starts this alongside the local server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from livekit import rtc
from livekit.agents import AgentServer, AgentSession, JobContext, JobProcess, cli, room_io
from livekit.plugins import anthropic, cartesia, deepgram, sarvam, silero

from dowel import line, store
from dowel.config import Settings, VoiceSettings
from dowel.gitops import git
from dowel.jev import Jev
from dowel.schemas import Usage
from dowel.store import WalkProgress
from dowel.tools import RepoTools
from dowel.voice import brief as briefs
from dowel.voice.guide import Guide, Walk, encode, keyterms, new_reader, speakable_stream, unpause_line
from dowel.voice.whole_turn import WholeTurnSTT

logger = logging.getLogger("dowel.voice")
settings = VoiceSettings()

# Claude as the talker: no hidden reasoning between turns, so a reply starts as soon as it can.
_CLAUDE_FAST = {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}


TOOL_RESULT_CHARS = 6000  # how much of a file a code look-up may return during a call


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=settings.vad_min_silence_s)


server = AgentServer(
    ws_url=settings.livekit_url,
    api_key=settings.livekit_key,
    api_secret=settings.livekit_secret,
    setup_fnc=prewarm,
    num_idle_processes=1,  # keeps one process warm so the first reply is not slow
)


def _turn_handling() -> dict[str, Any]:
    return {
        "turn_detection": "vad",
        "endpointing": {"mode": "fixed", "min_delay": settings.turn_min_delay_s,
                        "max_delay": settings.turn_max_delay_s},
        "interruption": {"mode": "adaptive", "min_duration": 0.35, "min_words": 0,
                         "false_interruption_timeout": 0.8, "resume_false_interruption": True},
        "preemptive_generation": {"enabled": True, "preemptive_tts": True, "max_speech_duration": 10.0,
                                  "max_retries": 3},
    }


def _talker() -> tuple[Any, dict[str, Any] | None]:
    if settings.talker == "sarvam":
        return sarvam.LLM(model=settings.sarvam_talk_model, max_tokens=300), None
    return anthropic.LLM(model=settings.talk_model, caching="ephemeral", max_tokens=400), _CLAUDE_FAST


def _speech(run: Any) -> tuple[Any, Any, dict[str, dict[str, str]]]:
    """The service that hears, the one that speaks, and how each reply's language is named to it.

    One place to change when swapping providers. The third value maps the two languages Dowel
    speaks ("en", "hi") to that service's own language code and voice.
    """
    if settings.speech == "gnani":
        try:
            from livekit.plugins import gnani
        except ImportError as exc:
            raise RuntimeError("Speech needs the Gnani plugin: uv sync, or uv add livekit-plugins-gnani") from exc
        voice = settings.gnani_voice  # one voice for both languages, so it never changes mid-conversation
        return (gnani.STT(language=settings.gnani_language, sample_rate=16000),
                gnani.TTS(voice=voice, model=settings.gnani_model, language="en-IN"),
                {"en": {"language": "en-IN", "voice": voice}, "hi": {"language": "hi-IN", "voice": voice}})
    return (
        deepgram.STT(model=settings.stt_model, language=settings.stt_language,
                     endpointing_ms=settings.stt_endpointing_ms, keyterm=keyterms(run)),
        cartesia.TTS(model=settings.tts_model, language=settings.tts_language, sample_rate=24000,
                     word_timestamps=False, voice=settings.tts_voice),
        {"en": {"language": "en", "voice": settings.tts_voice}, "hi": {"language": "hi", "voice": settings.hinglish_voice}},
    )


class _Screen:
    """Sends events to the page, one at a time, so it sees them in the order they happened."""

    def __init__(self, room: rtc.Room) -> None:
        self._room = room
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._drainer = asyncio.create_task(self._drain())

    def spawn(self, coro: Any) -> None:
        """Run something in the background and keep a reference so it is not collected mid-flight."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, topic: str, payload: dict[str, Any]) -> None:
        self._queue.put_nowait((topic, encode(payload)))

    def close(self) -> None:
        self._drainer.cancel()

    async def _drain(self) -> None:
        # Events raised while joining wait here instead of being lost. The room object offers no
        # "connected" event to await at this point, so this checks a few times a second.
        while not self._room.isconnected():  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        while True:
            topic, payload = await self._queue.get()
            try:
                await self._room.local_participant.send_text(payload, topic=topic)
            except Exception:
                logger.warning("could not send %s", topic, exc_info=True)


async def _watch_line(walk: Walk, session: AgentSession, screen: _Screen) -> None:
    """Keep the line open while this call is live, and say an agent's question out loud when one arrives."""
    seen: set[str] = set()
    while True:
        line.beat(str(walk.repo))
        for question in line.waiting(seen):
            seen.add(question["id"])
            if walk.paused or walk.hold is not None:
                continue  # one question at a time; the agent's own prompt takes over if this one waits too long
            walk.hold = question
            await screen.send("dowel.hold", question)
            await session.interrupt()
            session.say(line.spoken(question))
        await asyncio.sleep(0.5)


def _report_timings(session: AgentSession, screen: _Screen) -> None:
    """Per-turn timings for the strip on screen: heard, thought, spoke."""
    steps = {"EOUMetrics": ("heard", "transcription_delay"), "LLMMetrics": ("thought", "ttft"),
             "TTSMetrics": ("spoke", "ttfb")}

    def on_metrics(event: Any) -> None:
        metrics = getattr(event, "metrics", None)
        step = steps.get(type(metrics).__name__)
        if step:
            ms = int((getattr(metrics, step[1], 0) or 0) * 1000)
            screen.spawn(screen.send("dowel.timing", {"step": step[0], "ms": ms}))

    session.on("metrics_collected", on_metrics)


def _keep_transcript(session: AgentSession, walk: Walk) -> None:
    """Save what each side said, so a call can be picked up again later."""
    def on_item(event: Any) -> None:
        role, text = getattr(event.item, "role", None), getattr(event.item, "text_content", None)
        if role in ("assistant", "user") and text:
            walk.heard("dowel" if role == "assistant" else "you", text)

    session.on("conversation_item_added", on_item)


def _pause_and_resume(room: rtc.Room, walk: Walk, session: AgentSession, screen: _Screen) -> None:
    """The page's Pause and Resume buttons."""
    async def control(reader: rtc.TextStreamReader) -> None:
        try:
            action = json.loads(await reader.read_all()).get("action")
        except ValueError:
            return
        if action == "pause" and not walk.paused:
            walk.paused = True
            await session.interrupt()
            session.input.set_audio_enabled(False)
            await screen.send("dowel.paused", {"paused": True})
        elif action == "resume" and walk.paused:
            walk.paused = False
            session.input.set_audio_enabled(True)
            await screen.send("dowel.paused", {"paused": False})
            session.say(unpause_line(walk))

    room.register_text_stream_handler("dowel.control", lambda reader, _identity: screen.spawn(control(reader)))


def _typed_replies(guide: Guide) -> Any:
    """A typed reply gets the same hearing as a spoken one before the talker answers."""
    async def typed(sess: AgentSession, event: room_io.TextInputEvent) -> None:
        notes = await guide.take_in(event.text, typed=True)
        await sess.interrupt()
        if notes:
            sess.generate_reply(user_input=event.text, instructions=" ".join(notes))
        else:
            sess.generate_reply(user_input=event.text)

    return typed


async def _prepare(repo: Path, run: Any, fresh: bool, screen: _Screen) -> Walk:
    """Everything one walkthrough needs, with earlier progress if there is any to pick up."""
    saved = None if fresh else store.load_walk(str(repo))
    resumed = bool(saved and saved.transcript)
    merge_base = (await git(repo, "merge-base", run.base, "HEAD", check=False)).strip() or run.base
    walk = Walk(
        repo=repo, run=run, brief=await briefs.load_or_write(run, Settings()),
        tools=RepoTools(repo, merge_base, TOOL_RESULT_CHARS), send=screen.send, reader=new_reader(),
        settings=Settings(), progress=saved if resumed and saved else WalkProgress(), resumed=resumed,
    )
    # One fixed order everywhere: questions for the author first, then what Dowel found.
    rank = {f.id: (f.kind != "decision", n) for n, f in enumerate(run.findings)}
    walk.brief.items.sort(key=lambda item: rank.get(item.id, (True, 999)))
    walk.fast = Jev(Settings(), Usage())
    walk.save()
    return walk


@server.rtc_session(agent_name=settings.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    try:
        meta = json.loads(ctx.job.metadata or "{}")
        repo = Path(meta["repo"])
    except (KeyError, ValueError):
        logger.error("walkthrough rejected: no repository in the dispatch")
        return
    run = store.load_run(str(repo))
    if run is None:
        logger.error("walkthrough rejected: no saved review for %s", repo)
        return

    screen = _Screen(ctx.room)
    walk = await _prepare(repo, run, bool(meta.get("fresh")), screen)
    talker, llm_extra = _talker()
    listener, speaker, speaking = _speech(run)
    # Speech recognition finishes a sentence in pieces. Without this wrapper a pause mid-sentence
    # is taken as the whole turn and Dowel starts talking over the rest.
    hearing = WholeTurnSTT(listener)
    session = AgentSession[Walk](
        stt=hearing, llm=talker, tts=speaker, vad=ctx.proc.userdata["vad"], turn_handling=_turn_handling(),
        max_tool_steps=settings.max_tool_steps, tts_text_transforms=[speakable_stream], userdata=walk,
    )
    walk.session = session

    screen.spawn(_watch_line(walk, session, screen))
    _report_timings(session, screen)
    _keep_transcript(session, walk)
    _pause_and_resume(ctx.room, walk, session, screen)
    session.on("user_state_changed", lambda event: hearing.speaker_talking(event.new_state == "speaking"))

    async def cleanup() -> None:
        line.hang_up()
        walk.save()
        screen.close()

    ctx.add_shutdown_callback(cleanup)
    logger.info("walkthrough started for %s (talker=%s, resumed=%s)", repo.name, settings.talker, walk.resumed)
    guide = Guide(walk, speaking, llm_extra)
    await session.start(
        agent=guide,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(pre_connect_audio_timeout=1.0),
            text_input=room_io.TextInputOptions(text_input_cb=_typed_replies(guide)),
        ),
    )
    await screen.send("dowel.stage", {"stage": walk.progress.stage})


if __name__ == "__main__":
    cli.run_app(server)
