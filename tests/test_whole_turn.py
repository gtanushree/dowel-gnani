"""A spoken turn reaches the agent in one piece, however the recogniser cuts it up."""

from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType

from dowel.voice.whole_turn import TurnGate


def final(text: str) -> SpeechEvent:
    return SpeechEvent(SpeechEventType.FINAL_TRANSCRIPT, "r", [SpeechData(language="en", text=text)])


def finals(events: list[SpeechEvent]) -> list[str]:
    return [e.alternatives[0].text for e in events if e.type == SpeechEventType.FINAL_TRANSCRIPT]


def test_pieces_are_held_until_the_speaker_has_really_finished() -> None:
    gate = TurnGate()
    gate.on_speaker(True)
    gate.on_event(SpeechEvent(SpeechEventType.START_OF_SPEECH))
    assert finals(gate.on_event(final("keep it, but"))) == []   # a pause mid-sentence: nothing released
    assert finals(gate.on_flush()) == []                        # the session asks; speech is still open
    assert finals(gate.on_event(final("only for staging"))) == []
    gate.on_event(SpeechEvent(SpeechEventType.END_OF_SPEECH))
    assert finals(gate.on_speaker(False)) == ["keep it, but only for staging"]


def test_a_lost_end_of_speech_does_not_hold_the_turn_for_ever() -> None:
    gate = TurnGate()
    gate.on_event(SpeechEvent(SpeechEventType.START_OF_SPEECH))
    gate.on_event(final("change it"))
    assert finals(gate.on_flush()) == [] and gate.waiting
    assert finals(gate.on_timeout()) == ["change it"]
