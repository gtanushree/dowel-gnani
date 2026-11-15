import { RoomAudioRenderer, RoomContext, useIsSpeaking, useTranscriptions, useVoiceAssistant } from "@livekit/components-react";
import { Room, RoomEvent } from "livekit-client";
import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { highlight } from "./highlight";

type Who = "dowel" | "you";
type Line = { who: Who; text: string };
type Part = { text: string; settled: boolean };
type Turn = { who: Who; parts: Part[] };
type CodeLine = { n: number; text: string; added: boolean };
type CodeView = { file: string; focus: number; lines: CodeLine[] };
type Item = { id: string; plain: string; ask: string; kind: string; memory: string; earlier: string; answer?: string; note?: string };
type Heard = { intent: string; confidence: number; ms: number };
type Settled = { id: string; plain: string; answer: string; note: string; heard: string };
type Hold = { id: string; file: string; symbol: string; decision: string; reason: string; change: string; answer?: "allowed" | "refused" };
type Check = { state: "checking" } | { state: "confirmed"; title: string; where: string };
type Timing = { heard?: number; thought?: number; spoke?: number };
type Progress = {
  started: boolean;
  finished: boolean;
  total: number;
  answered: number;
  next: string | null;
  transcript: Line[];
};

const TOPICS = [
  "dowel.stage", "dowel.code", "dowel.item", "dowel.answer", "dowel.paused",
  "dowel.heard", "dowel.checking", "dowel.checked", "dowel.finding", "dowel.timing",
  "dowel.hold", "dowel.hold_done", "dowel.noted",
] as const;
const DECIDED: Record<string, string> = { keep: "Keeping it", change: "Changing it", unsure: "Flagged for a reviewer" };
const HEARD_AS: Record<string, string> = {
  keep: "keep", change: "change", unsure: "not sure", question: "a question", correction: "a correction", other: "something else",
};
const NEAR = 4; // lines either side of the one Dowel is talking about that stay at full strength

function heardText(h: Heard): string {
  return `heard as ${HEARD_AS[h.intent] ?? h.intent} · ${h.confidence.toFixed(2)} · ${h.ms} ms`;
}

function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function Ring({ tone, live }: { tone: Who | "idle"; live: boolean }) {
  return (
    <svg aria-hidden="true" className={`ring ${tone}${live ? " live" : ""}`} width="44" height="44" viewBox="0 0 44 44" fill="none">
      <circle className="outer" cx="22" cy="22" r="20" strokeWidth="1.5" />
      <circle className="mid" cx="22" cy="22" r="13" strokeWidth="1.5" />
      <circle className="core" cx="22" cy="22" r="6" />
    </svg>
  );
}

function Tick() {
  return (
    <svg aria-hidden="true" className="tick" width="16" height="16" viewBox="0 0 24 24" fill="none" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 12l5 5 9-10" />
    </svg>
  );
}

function Code({ view }: { view: CodeView | null }) {
  const focusRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    focusRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [view]);

  if (!view) {
    return (
      <div className="code blank">
        <p className="muted">Code appears here as Dowel talks about it.</p>
      </div>
    );
  }
  const added = view.lines.filter((l) => l.added).length;
  return (
    <div className="code">
      <div className="codehead mono">
        <span className="path">
          {view.file}
          <span className="muted">
            {" "}· line {view.focus}
            {added > 0 ? ` · ${added} ${added === 1 ? "line" : "lines"} changed here` : ""}
          </span>
        </span>
        <span className="here">
          <span aria-hidden="true" className="pip" />
          dowel is here
        </span>
      </div>
      <div className="codebody mono">
        {view.lines.map((l) => {
          const focus = l.n === view.focus;
          const far = Math.abs(l.n - view.focus) > NEAR;
          return (
            <div
              key={l.n}
              ref={focus ? focusRef : undefined}
              className={["cl", l.added ? "added" : "", focus ? "focus" : "", far ? "far" : ""].filter(Boolean).join(" ")}
            >
              <span className="ln">{l.n}</span>
              <span className="mark">{focus ? "▸" : l.added ? "+" : ""}</span>
              <span className="src">
                {l.text
                  ? highlight(view.file, l.text).map((t, i) => (t.kind ? <span key={i} className={t.kind}>{t.text}</span> : t.text))
                  : " "}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Session({ room, micOff, earlier, start, onLeave }: { room: Room; micOff: boolean; earlier: Line[]; start: { total: number; settled: number }; onLeave: () => void }) {
  const { state: agentState } = useVoiceAssistant();
  const streams = useTranscriptions({ room });
  const talking = useIsSpeaking(room.localParticipant);
  const [code, setCode] = useState<CodeView | null>(null);
  const [item, setItem] = useState<Item | null>(null);
  const [heard, setHeard] = useState<Heard | null>(null);
  const [check, setCheck] = useState<Check | null>(null);
  const [hold, setHold] = useState<Hold | null>(null);
  const [noted, setNoted] = useState<{ decision: string; file: string } | null>(null);
  const [settled, setSettled] = useState<Settled[]>([]);
  const itemRef = useRef<Item | null>(null);
  const heardRef = useRef<Heard | null>(null);
  const [timing, setTiming] = useState<Timing>({});
  const [counts, setCounts] = useState(start);
  const [paused, setPaused] = useState(false);
  const [muted, setMuted] = useState(micOff);
  const [typing, setTyping] = useState(micOff);
  const [draft, setDraft] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [typed, setTyped] = useState<{ text: string; after: number }[]>([]);
  const endRef = useRef<HTMLDivElement | null>(null);
  const settledIds = useRef(new Set<string>());

  useEffect(() => {
    const timer = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const handle = async (topic: string, text: string) => {
      const event = JSON.parse(text);
      if (topic === "dowel.paused") setPaused(event.paused);
      if (topic === "dowel.hold") setHold(event);
      if (topic === "dowel.hold_done") {
        setHold((h) => (h && h.id === event.id ? { ...h, answer: event.allow ? "allowed" : "refused" } : h));
        window.setTimeout(() => setHold((h) => (h && h.id === event.id ? null : h)), 6000);
      }
      if (topic === "dowel.noted") {
        setNoted({ decision: event.decision, file: event.file });
        window.setTimeout(() => setNoted(null), 8000);
      }
      if (topic === "dowel.timing") setTiming((t) => ({ ...t, [event.step]: event.ms }));
      if (topic === "dowel.heard") {
        heardRef.current = { intent: event.intent, confidence: event.confidence, ms: event.ms };
        setHeard(heardRef.current);
      }
      if (topic === "dowel.checking") setCheck({ state: "checking" });
      if (topic === "dowel.checked") setCheck(null);
      if (topic === "dowel.finding") setCheck({ state: "confirmed", title: event.title, where: `${event.file}:${event.line}` });
      if (topic === "dowel.item") {
        setHeard(null);
        heardRef.current = null;
        itemRef.current = { id: event.id, plain: event.plain, ask: event.ask, kind: event.kind, memory: event.memory ?? "", earlier: event.earlier ?? "" };
        setItem(itemRef.current);
        if (typeof event.total === "number") setCounts({ total: event.total, settled: event.total - event.open });
      }
      if (topic === "dowel.answer") {
        setItem((current) => (current && current.id === event.id ? { ...current, answer: event.answer, note: event.note } : current));
        // Keep what was decided on screen after Dowel moves on, newest first.
        const now = itemRef.current;
        const about = now && now.id === event.id ? now.plain : "";
        const h = heardRef.current;
        const line = h && h.intent === event.answer ? heardText(h) : "";
        setSettled((list) => {
          const had = list.find((s) => s.id === event.id);
          const entry = { id: event.id, plain: about || had?.plain || "", answer: event.answer, note: event.note ?? "", heard: line || had?.heard || "" };
          return [entry, ...list.filter((s) => s.id !== event.id)];
        });
        if (!settledIds.current.has(event.id)) {
          settledIds.current.add(event.id);
          setCounts((c) => ({ ...c, settled: Math.min(c.total, c.settled + 1) }));
        }
      }
      if (topic === "dowel.code") {
        const query = new URLSearchParams({ file: event.file, start: String(event.start), end: String(event.end) });
        const response = await fetch(`/api/code?${query}`);
        if (response.ok) setCode({ ...(await response.json()), focus: event.focus });
      }
    };
    for (const topic of TOPICS) {
      room.registerTextStreamHandler(topic, async (reader) => {
        try {
          await handle(topic, await reader.readAll());
        } catch {
          // A screen update that fails to arrive must not disturb the conversation.
        }
      });
    }
    return () => {
      for (const topic of TOPICS) room.unregisterTextStreamHandler(topic);
    };
  }, [room]);

  const spoken = useMemo(() => {
    const mine = room.localParticipant.identity;
    return streams
      .filter((s) => s.text.trim())
      .map((s) => ({
        who: s.participantInfo.identity === mine ? ("you" as const) : ("dowel" as const),
        text: s.text,
        settled: s.streamInfo.attributes?.["lk.transcription_final"] === "true",
      }));
  }, [streams, room]);
  const spokenCount = useRef(0);
  spokenCount.current = spoken.length;

  // Typed and tapped replies are not transcribed, so they are slotted in where they were sent.
  // Pieces from the same speaker in a row are one turn; a piece still being heard is drawn fainter.
  const turns: Turn[] = useMemo(() => {
    const out: Turn[] = [];
    const push = (who: Who, part: Part) => {
      const last = out[out.length - 1];
      if (last && last.who === who) last.parts.push(part);
      else out.push({ who, parts: [part] });
    };
    spoken.forEach((line, i) => {
      typed.filter((t) => t.after === i).forEach((t) => push("you", { text: t.text, settled: true }));
      push(line.who, { text: line.text, settled: line.settled });
    });
    typed.filter((t) => t.after >= spoken.length).forEach((t) => push("you", { text: t.text, settled: true }));
    return out;
  }, [spoken, typed]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns, talking]);

  const say = useCallback(
    (text: string) => {
      setTyped((list) => [...list, { text, after: spokenCount.current }]);
      void room.localParticipant.sendText(text, { topic: "lk.chat" });
    },
    [room],
  );

  const control = useCallback(
    (action: "pause" | "resume") => {
      void room.localParticipant.sendText(JSON.stringify({ action }), { topic: "dowel.control" });
    },
    [room],
  );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.trim()) say(draft.trim());
    setDraft("");
  };

  const toggleMute = () => {
    void room.localParticipant.setMicrophoneEnabled(muted);
    setMuted(!muted);
  };

  const mine = talking && !muted && !paused;
  const presence: { tone: Who | "idle"; live: boolean; title: string; hint: string } = paused
    ? { tone: "idle", live: false, title: "Paused", hint: "dowel is not listening" }
    : mine
      ? { tone: "you", live: true, title: "You're speaking", hint: "dowel is listening and will wait for you to finish" }
      : agentState === "speaking"
        ? { tone: "dowel", live: true, title: "Dowel is speaking", hint: "talk over it any time to cut in" }
        : agentState === "thinking"
          ? { tone: "dowel", live: false, title: "Dowel is thinking", hint: "one moment" }
          : agentState === "listening"
            ? muted
              ? { tone: "idle", live: false, title: "Your microphone is off", hint: "type your reply below" }
              : { tone: "you", live: false, title: "Your turn", hint: "take your time" }
            : { tone: "idle", live: false, title: "Connecting to Dowel", hint: "this takes a few seconds" };

  // The newest turn is drawn large beside the presence mark, unless you have started talking and
  // your words have not arrived yet: then it belongs with the history.
  const last = turns[turns.length - 1];
  const whole = last && !(mine && last.who === "dowel") ? last : null;
  // Dowel speaks in several sentences; only the one being said is large, the rest join the history.
  const split = whole && whole.who === "dowel" && whole.parts.length > 1;
  const current = whole && split ? { who: whole.who, parts: whole.parts.slice(-1) } : whole;
  const history = whole
    ? [...turns.slice(0, -1), ...(split ? [{ who: whole.who, parts: whole.parts.slice(0, -1) }] : [])]
    : turns;
  const position = Math.min(counts.total, counts.settled + 1);
  const heardLine = heard ? heardText(heard) : "";
  const before = settled.filter((s) => s.id !== item?.id).slice(0, 2);

  return (
    <div className="talk">
      <header className="bar">
        <span className="brand">dowel</span>
        {counts.total > 0 && (
          <div className="pips" aria-label={`${counts.settled} of ${counts.total} settled`}>
            {Array.from({ length: counts.total }, (_, i) => (
              <span key={i} className={i < counts.settled ? "done" : ""} />
            ))}
            <span className="mono muted">
              {counts.settled} of {counts.total} settled
            </span>
          </div>
        )}
        <div className="meter mono muted">
          {(["heard", "thought", "spoke"] as const).map((step) =>
            timing[step] !== undefined ? (
              <span key={step}>
                {step} <b>{timing[step]} ms</b>
              </span>
            ) : null,
          )}
          <span>private · {clock(elapsed)}</span>
        </div>
      </header>

      <main className="split">
        <section className="convo" aria-label="Conversation">
          <div className="history" aria-live="polite">
            {earlier.length > 0 && (
              <>
                {earlier.map((line, i) => (
                  <div key={`e${i}`} className="turn past">
                    <span className="mono who">{line.who}</span>
                    <p>{line.text}</p>
                  </div>
                ))}
                <div className="mono muted divider">picked up here</div>
              </>
            )}
            {history.map((turn, i) => (
              <div key={i} className={`turn past${i === history.length - 1 ? " recent" : ""}`}>
                <span className="mono who">{turn.who}</span>
                <p>{turn.parts.map((p) => p.text).join(" ")}</p>
              </div>
            ))}
            <div className={`live ${presence.tone}`}>
              <div className="presence">
                <Ring tone={presence.tone} live={presence.live} />
                <div>
                  <div className="ptitle">{presence.title}</div>
                  <div className="mono muted">{presence.hint}</div>
                </div>
              </div>
              {current && (
                <p className="saying">
                  {current.parts.map((p, i) => (
                    <span key={i} className={current.who === "you" && !p.settled && mine ? "unsettled" : undefined}>
                      {p.text}{" "}
                    </span>
                  ))}
                </p>
              )}
            </div>
            <div ref={endRef} />
          </div>
        </section>

        <section className="screen" aria-label="What Dowel is showing">
          {hold && (
            <div className="now agent" role="status">
              <span className="mono frommemory">
                {hold.answer ? `you ${hold.answer} it · the agent has been told` : "your agent is waiting for you"}
              </span>
              <p className="question">
                It wants to change <span className="mono">{hold.symbol || hold.file}</span>.{" "}
                <span className="ask">You decided to keep this: {hold.decision}</span>
              </p>
              {hold.reason && <p className="earlier">"{hold.reason}"</p>}
              {hold.change && <pre className="mono change">{hold.change}</pre>}
              {!hold.answer &&
                (muted || typing ? (
                  <div className="controls">
                    <button type="button" className="solid" onClick={() => say("Yes, allow it.")}>
                      Allow it
                    </button>
                    <button type="button" className="solid" onClick={() => say("No, refuse it. Keep it as it is.")}>
                      Refuse it
                    </button>
                  </div>
                ) : (
                  <span className="mono hint">say "allow it" or "refuse it"</span>
                ))}
            </div>
          )}
          {noted && (
            <div className="toast" role="status">
              <Tick />
              <span className="what">On record</span>
              <span className="said">{noted.decision}</span>
              <span className="mono muted where">{noted.file}</span>
            </div>
          )}
          {item && !hold && (
            <div className="now">
              <span className={`mono ${item.memory ? "frommemory" : "muted"}`}>
                {item.memory
                  ? `from memory · ${item.memory}`
                  : item.kind === "decision"
                    ? `for you · ${position} of ${counts.total}`
                    : `dowel found · ${position} of ${counts.total}`}
              </span>
              <p className="question">
                {item.plain} <span className="ask">{item.ask}</span>
              </p>
              {item.earlier && <p className="earlier">{item.earlier}</p>}
              {item.answer ? (
                <div className="captured">
                  <Tick />
                  <span className="what">{DECIDED[item.answer] ?? item.answer}</span>
                  {item.note && <span className="said">"{item.note}"</span>}
                  {heard && heard.intent === item.answer && <span className="mono muted">{heardLine}</span>}
                </div>
              ) : (
                <>
                  {heard && (
                    <div className="captured pending">
                      <span aria-hidden="true" className="open" />
                      <span className="mono">{heardLine}</span>
                    </div>
                  )}
                  {muted || typing ? (
                    <div className="controls">
                      <button type="button" className="solid" onClick={() => say(item.kind === "decision" ? "Keep it." : "Leave it as it is.")}>
                        {item.kind === "decision" ? "Keep it" : "Leave it"}
                      </button>
                      <button type="button" className="solid" onClick={() => say(item.kind === "decision" ? "Change it." : "Yes, fix it.")}>
                        {item.kind === "decision" ? "Change it" : "Fix it"}
                      </button>
                      <button type="button" className="solid" onClick={() => say("I'm not sure. Flag it for the reviewer.")}>
                        Not sure
                      </button>
                    </div>
                  ) : (
                    <span className="mono hint">
                      {item.memory
                        ? 'say "changed my mind" or "that\'s a slip"'
                        : 'say "keep it", "change it" or "not sure", or just talk it through'}
                    </span>
                  )}
                </>
              )}
            </div>
          )}
          {before.length > 0 && (
            <div className="settled" aria-label="Decided so far">
              {before.map((s) => (
                <div key={s.id} className="captured">
                  <Tick />
                  <span className="what">{DECIDED[s.answer] ?? s.answer}</span>
                  {s.note ? <span className="said">"{s.note}"</span> : <span className="about">{s.plain}</span>}
                  {s.heard && <span className="mono muted">{s.heard}</span>}
                </div>
              ))}
            </div>
          )}
          <Code view={code} />
          {check && (
            <div className="toast" role="status">
              {check.state === "checking" ? (
                <span className="muted">Checking the code again with your answer.</span>
              ) : (
                <>
                  <Tick />
                  <span className="what">Confirmed after your answer</span>
                  <span className="said">{check.title}</span>
                  <span className="mono muted where">{check.where} · added to fixes</span>
                </>
              )}
            </div>
          )}
        </section>
      </main>

      <footer className="dock">
        {typing && !paused ? (
          <form className="typebox" onSubmit={submit}>
            <input
              autoFocus
              value={draft}
              aria-label="Type to Dowel"
              placeholder="Type to Dowel and press Enter"
              onChange={(e) => setDraft(e.target.value)}
            />
            <button type="button" className="quiet" onClick={() => setTyping(false)}>
              Back to voice
            </button>
          </form>
        ) : (
          <>
            <button type="button" className="solid" onClick={() => control(paused ? "resume" : "pause")}>
              {paused ? "Resume" : "Pause"}
            </button>
            {!paused && (
              <>
                <button type="button" className="quiet" aria-pressed={muted} onClick={toggleMute}>
                  {muted ? "Unmute" : "Mute"}
                </button>
                <button type="button" className="quiet" onClick={() => setTyping(true)}>
                  Type instead
                </button>
                <button type="button" className="quiet" onClick={() => say("Skip this one.")}>
                  Skip this one
                </button>
              </>
            )}
          </>
        )}
        <button type="button" className="quiet leave" onClick={onLeave}>
          Leave and finish later
        </button>
      </footer>
    </div>
  );
}

export function Talk() {
  const [room, setRoom] = useState<Room | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [earlier, setEarlier] = useState<Line[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [micOff, setMicOff] = useState(false);

  useEffect(() => {
    fetch("/api/talk/progress")
      .then((r) => (r.ok ? r.json() : null))
      .then(setProgress, () => setProgress(null));
  }, []);

  const start = async (fresh: boolean) => {
    setStarting(true);
    setError(null);
    try {
      const response = await fetch(`/api/talk/token?fresh=${fresh}`, { method: "POST" });
      if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail ?? "Could not start.");
      const { url, token } = await response.json();
      const next = new Room();
      next.on(RoomEvent.Disconnected, () => window.location.assign("/"));
      await next.connect(url, token);
      try {
        await next.localParticipant.setMicrophoneEnabled(true);
      } catch {
        setMicOff(true); // no microphone or permission refused: carry on by typing
      }
      setEarlier(fresh ? [] : (progress?.transcript ?? []));
      setRoom(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start.");
      setStarting(false);
    }
  };

  const leave = useCallback(() => {
    void room?.disconnect();
  }, [room]);

  if (!room) {
    const resuming = progress?.started && !progress.finished;
    return (
      <div className="empty">
        <h1>{resuming ? "Pick up where you left off." : "Talk this change through with Dowel."}</h1>
        {resuming && progress ? (
          <p className="muted">
            {progress.answered} of {progress.total} settled.
            {progress.next ? ` Next up: ${progress.next}` : ""} Dowel will say where you were before carrying on.
          </p>
        ) : (
          <p className="muted">
            About five minutes. Dowel explains what the code now does and asks what you want. Pause or leave any
            time and pick up later. Your answers land on the proof sheet, and nothing is shared until you copy it.
          </p>
        )}
        <p className="muted">
          Your voice goes to a speech service to be transcribed. The audio is not kept; the words are saved on
          this computer so you can resume.
        </p>
        <div className="controls">
          <button type="button" className="btn primary wide" disabled={starting} onClick={() => start(false)}>
            {starting ? "Connecting" : resuming ? "Resume" : "Start talking"}
          </button>
          {resuming && (
            <button type="button" className="quiet" disabled={starting} onClick={() => start(true)}>
              Start over
            </button>
          )}
          <a className="quiet aslink" href="/">
            Answer on the page instead
          </a>
        </div>
        {error && <p role="alert">{error}</p>}
      </div>
    );
  }

  return (
    <RoomContext.Provider value={room}>
      <RoomAudioRenderer />
      <Session
        room={room}
        micOff={micOff}
        earlier={earlier}
        start={{ total: progress?.total ?? 0, settled: earlier.length > 0 ? (progress?.answered ?? 0) : 0 }}
        onLeave={leave}
      />
    </RoomContext.Provider>
  );
}
