"""Keeps a speaker's turn in one piece: the STT's final transcripts are held until the turn is over.

LiveKit commits a turn with whatever final transcript it holds when its end-of-turn delay runs
out. Deepgram finalises speech in pieces, the last piece about 0.45-0.8 s after the speaker stops
(measured in an earlier project), so a turn committed earlier carried only its first pieces and the last one
arrived as a second user message: the agent answered half a sentence, then answered again. With no
final transcript at all, LiveKit waits for the next one instead of committing. So the finals
are held here (and passed on as interim text for captions) and released as one final when
three things are true: LiveKit has asked for the turn's transcript by flushing the stream at its
end of turn, the STT has finalised everything it heard, and the speaker is silent.
"""

import asyncio
import time
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from livekit import rtc
from livekit.agents import APIConnectOptions, ConversationItemAddedEvent, stt, utils
from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

# Once LiveKit has asked, how long to wait for the STT to end its speech before releasing what
# is held anyway (a lost end-of-speech must not hold the turn for ever). At 100 ms endpointing
# Deepgram's end of speech came 0.53-0.57 s after speech (p90 0.72 s on noisy clips), about 0.2 s
# after LiveKit asks.
PENDING_TIMEOUT_S = 0.6


@dataclass
class TurnGate:
    """The holding rule, without I/O: each method returns the events to pass on to LiveKit."""

    held: list[SpeechData] = field(default_factory=list[SpeechData])
    # Between the STT's start and end of speech: it may still finalise more words.
    in_speech: bool = False
    # Interim words the STT has not finalised yet.
    unfinal: bool = False
    # LiveKit flushed the stream: it wants this turn's transcript.
    asked: bool = False
    speaker_talking: bool = False
    request_id: str = ""

    @property
    def pending(self) -> bool:
        return self.in_speech or self.unfinal

    @property
    def waiting(self) -> bool:
        """Held text is owed a final the STT has not sent yet."""
        return bool(self.held) and self.asked and self.pending and not self.speaker_talking

    def on_event(self, event: SpeechEvent) -> list[SpeechEvent]:
        text = event.alternatives[0].text if event.alternatives else ""
        if event.type == SpeechEventType.START_OF_SPEECH:
            self.in_speech = True
        elif event.type == SpeechEventType.END_OF_SPEECH:
            self.in_speech = False
            return [event, *self._release()]
        elif event.type == SpeechEventType.INTERIM_TRANSCRIPT and text:
            self.unfinal = True
            return [self._interim(event, [*self._texts(), text])]
        elif event.type == SpeechEventType.FINAL_TRANSCRIPT and text:
            self.held.append(event.alternatives[0])
            self.request_id = event.request_id
            self.unfinal = False
            return [self._interim(event, self._texts()), *self._release()]
        return [event]

    def on_flush(self) -> list[SpeechEvent]:
        self.asked = True
        return self._release()

    def on_speaker(self, speaking: bool) -> list[SpeechEvent]:
        self.speaker_talking = speaking
        if speaking:
            # A flush answered a pause the speaker has now ended; the next pause brings a new one.
            self.asked = False
            return []
        return self._release()

    def on_timeout(self) -> list[SpeechEvent]:
        self.in_speech = self.unfinal = False
        return self._release()

    def _texts(self) -> list[str]:
        return [data.text for data in self.held]

    def _interim(self, event: SpeechEvent, texts: list[str]) -> SpeechEvent:
        first = self.held[0] if self.held else event.alternatives[0]
        data = replace(first, text=" ".join(texts))
        return SpeechEvent(SpeechEventType.INTERIM_TRANSCRIPT, event.request_id, [data])

    def _release(self) -> list[SpeechEvent]:
        if not self.held or not self.asked or self.pending or self.speaker_talking:
            return []
        final = SpeechEvent(SpeechEventType.FINAL_TRANSCRIPT, self.request_id, [merged(self.held)])
        # `asked` stays set until the speaker talks again: a final that arrives after a timeout
        # release goes out at once rather than into the speaker's next turn (seen at 500 ms).
        self.held = []
        return [final]


def merged(pieces: list[SpeechData]) -> SpeechData:
    words = [word for piece in pieces for word in piece.words or []]
    return replace(
        pieces[-1],
        text=" ".join(piece.text for piece in pieces),
        start_time=pieces[0].start_time,
        confidence=sum(piece.confidence for piece in pieces) / len(pieces),
        words=words if all(piece.words for piece in pieces) else None,
    )


class WholeTurnSTT(stt.STT[Any]):
    """Wraps a streaming STT so each speaker turn reaches LiveKit as one final transcript."""

    def __init__(self, inner: stt.STT[Any]) -> None:
        super().__init__(capabilities=replace(inner.capabilities, manual_flush=True))
        self._inner = inner
        self._streams: weakref.WeakSet[WholeTurnStream] = weakref.WeakSet()

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    # Session key terms and chat context belong to the wrapped STT, as in LiveKit's own adapters.
    def _update_session_keyterms(self, keyterms: list[str]) -> None:
        self._inner._update_session_keyterms(keyterms)

    def _push_conversation_item(self, ev: ConversationItemAddedEvent) -> None:
        self._inner._push_conversation_item(ev)

    def speaker_talking(self, speaking: bool) -> None:
        """The session's VAD state, from `user_state_changed`."""
        for stream in list(self._streams):
            stream.speaker_talking(speaking)

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> SpeechEvent:
        return await self._inner.recognize(buffer, language=language, conn_options=conn_options)

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "WholeTurnStream":
        stream = WholeTurnStream(self, self._inner, language=language, conn_options=conn_options)
        self._streams.add(stream)
        return stream

    async def aclose(self) -> None:
        await self._inner.aclose()


class WholeTurnStream(stt.RecognizeStream):
    def __init__(
        self,
        owner: WholeTurnSTT,
        inner: stt.STT[Any],
        *,
        language: NotGivenOr[str],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(stt=owner, conn_options=conn_options)
        self._inner_stt = inner
        self._language = language
        self._gate = TurnGate()
        self._timeout: asyncio.TimerHandle | None = None

    def speaker_talking(self, speaking: bool) -> None:
        self._send(self._gate.on_speaker(speaking))

    async def _run(self) -> None:
        inner = self._inner_stt.stream(language=self._language, conn_options=self._conn_options)
        forward = asyncio.create_task(self._forward(inner))
        try:
            async for event in inner:
                self._send(self._gate.on_event(event))
        finally:
            if self._timeout is not None:
                self._timeout.cancel()
            await utils.aio.cancel_and_wait(forward)
            await inner.aclose()

    async def _forward(self, inner: stt.RecognizeStream) -> None:
        seeded = False
        async for item in self._input_ch:
            if isinstance(item, rtc.AudioFrame):
                # Word times come back on the session's timeline, as with the bare STT.
                if not seeded and (origin := self._input_ch.input_started_at) is not None:
                    inner.start_time_offset = time.time() - origin
                    seeded = True
                inner.push_frame(item)
            else:
                # Not passed on to the STT: a Deepgram Finalize sent in a pause the speaker then
                # ended cut the next word in testing; Deepgram's own endpointing cuts only at silence.
                self._send(self._gate.on_flush())
        inner.end_input()

    def _send(self, events: Iterable[SpeechEvent]) -> None:
        for event in events:
            if not self._event_ch.closed:
                self._event_ch.send_nowait(event)
        if self._timeout is not None:
            self._timeout.cancel()
            self._timeout = None
        if self._gate.waiting:
            self._timeout = asyncio.get_running_loop().call_later(
                PENDING_TIMEOUT_S, lambda: self._send(self._gate.on_timeout())
            )
