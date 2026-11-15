"""Settings and budgets. Keys come from the environment or a .env beside the Dowel checkout."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_DOWEL_HOME = Path(__file__).resolve().parents[2]
load_dotenv(_DOWEL_HOME / ".env")
# Never from the current directory: that is the repository being reviewed, and a .env there could
# point the model client at someone else's server and hand it the key.
load_dotenv(Path.home() / ".dowel" / ".env")


@dataclass(frozen=True)
class Models:
    """Claude is the only generator. Each stage uses the cheapest tier that does its job."""

    propose: str = "claude-sonnet-5-5"  # ideas about what might be broken, one call per part
    investigate: str = "claude-haiku-5-5"  # many parallel, narrow, tool-using checks
    verify: str = "claude-sonnet-5-5"  # tries to refute each confirmed finding
    edit: str = "claude-opus-5-5"  # one call: merges, sorts and words the final list
    assist: str = "claude-sonnet-5-5"  # change summary, the voice brief, code look-ups in a call
    jev: str = "jev-latest"  # closed questions


@dataclass(frozen=True)
class Budgets:
    max_hypotheses: int = 15
    max_investigators: int = 15
    tool_calls_per_investigator: int = 8
    tool_calls_per_verifier: int = 4
    max_verifier_calls: int = 8
    max_questions: int = 5
    max_issues: int = 7  # restraint: the strongest few, the rest are kept as background
    parallel: int = 6
    diff_chars: int = 120_000
    part_chars: int = 90_000  # how much diff one part holds
    max_parts: int = 16
    max_total_investigators: int = 48
    max_total_verifier_calls: int = 24
    cost_cap_per_extra_part_usd: float = 0.4
    investigator_diff_chars: int = 24_000  # one investigator sees its own file's change, not the part
    context_chars: int = 60_000
    tool_result_chars: int = 12_000
    cost_cap_usd: float = 2.0  # a review stops, and says so, when it reaches this


@dataclass(frozen=True)
class JevBars:
    evidence_supports: float = 0.25  # below this the evidence gate drops the finding; the verifier sees the rest
    confident: float = 0.7  # below this Claude's own ruling stands


@dataclass(frozen=True)
class Settings:
    models: Models = field(default_factory=Models)
    budgets: Budgets = field(default_factory=Budgets)
    jev_bars: JevBars = field(default_factory=JevBars)
    typesafe_base_url: str = "https://api.typesafe.ai"

    @property
    def typesafe_key(self) -> str | None:
        return os.environ.get("TYPESAFE_API_KEY") or None

    def fast(self) -> Settings:
        """Fewer, cheaper checks for a quick pass."""
        return Settings(
            models=self.models,
            budgets=Budgets(
                max_hypotheses=8,
                max_investigators=8,
                tool_calls_per_investigator=5,
                tool_calls_per_verifier=2,
                max_verifier_calls=5,
            ),
            jev_bars=self.jev_bars,
            typesafe_base_url=self.typesafe_base_url,
        )


# USD per million tokens (input, output). Cache reads are billed at a tenth of input here,
# cache writes at 1.25x; close enough for a per-run ledger.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def keep_local() -> bool:
    """Whether answers stay on this machine, with nothing written to the repository."""
    return os.environ.get("DOWEL_KEEP_LOCAL") == "1"


def slack_webhook() -> str:
    """Where `dowel brief --slack` posts, or an empty string."""
    return os.environ.get("DOWEL_SLACK_WEBHOOK", "")


@dataclass(frozen=True)
class VoiceSettings:
    """Defaults carried over from earlier voice experiments."""

    talk_model: str = "claude-sonnet-5-5"  # used when DOWEL_TALKER=claude
    sarvam_talk_model: str = "sarvam-105b-conversations"  # plain sarvam-105b reasons first: too slow to speak
    # One Indian voice for both English and Hinglish, so the voice never changes mid-conversation;
    # only the language setting switches per reply. "Rehan", a male Hindi-native Cartesia voice.
    # Samples of others are in voice-samples/. Set DOWEL_VOICE_ID to change it.
    indian_voice: str = "205fc552-2cce-4307-baa1-598b9dc3dd01"
    stt_model: str = "nova-3"
    stt_language: str = "multi"
    stt_endpointing_ms: int = 300
    tts_model: str = "sonic-3.6-2026-08-27"
    tts_language: str = "en"
    vad_min_silence_s: float = 0.35
    # People pause to think about code, so these are longer than a sales call needs.
    turn_min_delay_s: float = 1.2
    turn_max_delay_s: float = 4.0
    max_tool_steps: int = 3

    @property
    def livekit_url(self) -> str | None:
        return os.environ.get("LIVEKIT_URL") or None

    @property
    def livekit_key(self) -> str | None:
        return os.environ.get("LIVEKIT_API_KEY") or None

    @property
    def livekit_secret(self) -> str | None:
        return os.environ.get("LIVEKIT_API_SECRET") or None

    @property
    def talker(self) -> str:
        """Which model does the talking: sarvam (fast, the default when its key is set) or claude."""
        choice = (os.environ.get("DOWEL_TALKER") or "").lower()
        if choice in ("sarvam", "claude"):
            return choice
        return "sarvam" if os.environ.get("SARVAM_API_KEY") else "claude"

    @property
    def tts_voice(self) -> str:
        return os.environ.get("DOWEL_VOICE_ID") or self.indian_voice

    @property
    def hinglish_voice(self) -> str:
        return os.environ.get("DOWEL_HINGLISH_VOICE_ID") or self.tts_voice

    @property
    def agent_name(self) -> str:
        # The LiveKit project is shared with other apps, and a call goes to any worker with the
        # same name, so each machine gets its own.
        import hashlib
        import socket

        return os.environ.get("DOWEL_AGENT_NAME") or "dowel-" + hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]

    @property
    def speech(self) -> str:
        """Who listens and speaks: "gnani" (Vachana hears, Timbre speaks) unless told otherwise.

        Gnani is the speech stack. DOWEL_SPEECH=deepgram switches to the older pair (Deepgram hears,
        Cartesia speaks), kept only so the two can be compared.
        """
        return "default" if (os.environ.get("DOWEL_SPEECH") or "").lower() in ("deepgram", "default") else "gnani"

    @property
    def gnani_voice(self) -> str:
        # The plugin's voices: Pranav, Deepak (male), Kaveri, Shubhra (female).
        return os.environ.get("DOWEL_GNANI_VOICE") or "Pranav"

    @property
    def gnani_model(self) -> str:
        # The plugin's own default, timbre-v2.0, has been retired by Gnani. timbre-v2.5 works with the
        # plugin's voices. timbre-v3.0 has a different set (Avinash, Shubham, Prakash, Shivani, Lakshmi, ...).
        return os.environ.get("DOWEL_GNANI_MODEL") or "timbre-v2.5"

    @property
    def gnani_language(self) -> str:
        # One locale only. Gnani's service can hear several at once, but only through a setting
        # ("multi_lang_codes") that its LiveKit plugin does not send, and a combined code such as
        # "en-IN,hi-IN" is refused when streaming. Checked against the live service, November 2026.
        chosen = os.environ.get("DOWEL_GNANI_LANGUAGE") or "en-IN"
        return chosen.split(",")[0].strip()

    def missing(self) -> list[str]:
        speech = ["GNANI_API_KEY"] if self.speech == "gnani" else ["DEEPGRAM_API_KEY", "CARTESIA_API_KEY"]
        needed = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", *speech, "ANTHROPIC_API_KEY"]
        return [name for name in needed if not os.environ.get(name)]
