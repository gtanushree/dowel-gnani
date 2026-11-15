import { useCallback, useEffect, useRef, useState } from "react";

import {
  type Answer,
  type ChangeMap,
  type Finding,
  getChange,
  getExport,
  getSheet,
  type ItemPatch,
  type ItemState,
  patchItem,
  type Sheet,
} from "./api";

const ANSWERS: { value: Answer; label: string }[] = [
  { value: "keep", label: "Keep it" },
  { value: "change", label: "Change it" },
  { value: "unsure", label: "Not sure" },
];

function Trace({ finding }: { finding: Finding }) {
  const evidence = finding.verdict.evidence.slice(0, 2);
  return (
    <dl className="trace mono">
      <div>
        <dt>idea</dt>
        <dd>{finding.hypothesis.statement}</dd>
      </div>
      {finding.searched.length > 0 && (
        <div>
          <dt>looked at</dt>
          <dd>{finding.searched.slice(0, 6).join(", ")}</dd>
        </div>
      )}
      {evidence.map((e) => (
        <div key={`${e.file}:${e.start_line}`}>
          <dt>evidence</dt>
          <dd className="quote">
            {e.file}:{e.start_line}
            {"\n"}
            {e.quote.trim()}
          </dd>
        </div>
      ))}
      <div>
        <dt>{finding.review ? "tried to refute" : "why it is open"}</dt>
        <dd>{finding.review ? finding.review.refutation_attempt : finding.verdict.reasoning}</dd>
      </div>
    </dl>
  );
}

function Item({
  finding,
  item,
  label,
  onChange,
}: {
  finding: Finding;
  item: ItemState;
  label: string;
  onChange: (patch: ItemPatch) => void;
}) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(item.note);
  const timer = useRef<number | undefined>(undefined);
  const off = !item.included || item.noise;

  const saveNote = (value: string) => {
    setNote(value);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => onChange({ note: value }), 500);
  };

  return (
    <div className={off ? "item off" : "item"}>
      <input
        type="checkbox"
        checked={item.included && !item.noise}
        disabled={item.noise}
        aria-label={`Share: ${finding.title}`}
        onChange={(e) => onChange({ included: e.target.checked })}
      />
      <div className="body">
        <span className="mono muted">{item.noise ? "you asked Dowel to stop raising this" : label}</span>
        <span className="title">
          {finding.title} {finding.kind === "decision" && !off && <span className="ask">{finding.question}</span>}
        </span>
        {finding.detail && <span className="detail">{finding.detail}</span>}
        {finding.memory && <span className="mono where">{finding.memory}</span>}
        <div className="row">
          <span className="mono where">
            {finding.file}:{finding.line}
          </span>
          <button type="button" className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide reasoning" : "Show reasoning"}
          </button>
          <button type="button" className="link" onClick={() => onChange({ noise: !item.noise })}>
            {item.noise ? "Undo" : "Stop raising this kind of thing"}
          </button>
        </div>
        {finding.kind === "decision" && (
          <div className="answer">
            <div className="choices" role="group" aria-label="Your answer">
              {ANSWERS.map((a) => (
                <button
                  key={a.value}
                  type="button"
                  className="choice"
                  aria-pressed={item.answer === a.value}
                  onClick={() => onChange(item.answer === a.value ? { clear_answer: true } : { answer: a.value })}
                >
                  {a.label}
                </button>
              ))}
            </div>
            {item.answer && (
              <textarea
                className="note"
                rows={1}
                value={note}
                aria-label="A note in your own words"
                placeholder={
                  item.answer === "change"
                    ? "What do you want instead?"
                    : item.answer === "keep"
                      ? "Why, in your words (optional)"
                      : "What would you want a reviewer to check? (optional)"
                }
                onChange={(e) => saveNote(e.target.value)}
              />
            )}
          </div>
        )}
        {open && <Trace finding={finding} />}
      </div>
    </div>
  );
}

function CopyButton({ kind, label, primary }: { kind: "summary" | "prompt" | "brief"; label: string; primary?: boolean }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(await getExport(kind));
      setState("done");
    } catch {
      setState("failed");
    }
    window.setTimeout(() => setState("idle"), 1800);
  };
  return (
    <button type="button" className={primary ? "btn primary" : "btn"} onClick={copy}>
      {state === "done" ? "Copied" : state === "failed" ? "Could not copy" : label}
    </button>
  );
}

const LABELS: ChangeMap["files"][number]["label"][] = ["not explained", "asked for", "needed for it", "mechanical"];
const KEY: Record<string, string> = {
  "not explained": "unexplained",
  "asked for": "asked",
  "needed for it": "needed",
  mechanical: "mechanical",
};

// The change as one bar: each file is a segment as wide as its share of the changed lines,
// and the files nobody asked for are the ones drawn to stand out.
function Map({ change }: { change: ChangeMap }) {
  const total = change.files.reduce((n, f) => n + f.added + f.removed, 0) || 1;
  const unexplained = change.files.filter((f) => f.label === "not explained");
  const count = (label: string) => change.files.filter((f) => f.label === label).length;
  return (
    <section className="map" aria-label="This change">
      <div className="maphead">
        <span className="mono muted">
          {change.source ? `what was asked, from ${change.source}` : "nothing recorded says what was asked for"}
        </span>
        {change.asked && <p className="asked">{change.asked}</p>}
      </div>
      <div className="mapbar" role="img" aria-label={`${change.files.length} files changed, ${unexplained.length} not explained`}>
        {change.files.map((f) => (
          <span
            key={f.path}
            className={`seg ${KEY[f.label]}`}
            style={{ flexGrow: Math.max(f.added + f.removed, total / 60) }}
            title={`${f.path} · ${f.label} · +${f.added} −${f.removed}`}
          />
        ))}
      </div>
      <div className="maplegend mono muted">
        {LABELS.filter((l) => count(l) > 0).map((l) => (
          <span key={l}>
            <i className={`seg ${KEY[l]}`} /> {count(l)} {l}
          </span>
        ))}
      </div>
      {unexplained.length > 0 && change.source && (
        <ul className="maplist">
          {unexplained.slice(0, 6).map((f) => (
            <li key={f.path}>
              <span className="mono where">{f.path}</span>
              <span className="muted">
                +{f.added} −{f.removed} · {f.why}
              </span>
            </li>
          ))}
        </ul>
      )}
      {change.problems.length > 0 && (
        <div className="needs">
          <span className="mono eyebrow">needs a person before this goes in</span>
          {change.problems.map((p, i) => (
            <div key={i} className="need">
              <span className="mono where">{p.where}</span>
              <p>{p.what}</p>
              {p.lines.slice(0, 2).map((line) => (
                <code key={line} className="mono muted">
                  {line}
                </code>
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function App() {
  const [sheet, setSheet] = useState<Sheet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [changeMap, setChangeMap] = useState<ChangeMap | null>(null);

  useEffect(() => {
    getSheet().then(setSheet, (e: Error) => setError(e.message));
    getChange().then(setChangeMap, () => setChangeMap(null)); // the review still shows without the map
  }, []);

  const change = useCallback((id: string, patch: ItemPatch) => {
    patchItem(id, patch).then(setSheet, (e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="empty">
        <h1>Dowel couldn't load this review.</h1>
        <p className="muted">{error}</p>
      </div>
    );
  }
  if (!sheet) {
    return null;
  }

  const { run, state, labels, why } = sheet;
  const shown = run.findings.filter((f) => f.kind !== "background");
  const decisions = shown.filter((f) => f.kind === "decision");
  const issues = shown.filter((f) => f.kind === "issue");
  const background = run.findings.filter((f) => f.kind === "background");
  const checked = run.findings.length + run.dropped.length;
  const sharing = shown.filter((f) => state.items[f.id]?.included && !state.items[f.id]?.noise).length;
  const unanswered = decisions.filter((f) => !state.items[f.id]?.answer && !state.items[f.id]?.noise).length;

  const render = (f: Finding) => (
    <Item key={f.id} finding={f} item={state.items[f.id]} label={labels[f.id]} onChange={(p) => change(f.id, p)} />
  );

  return (
    <>
      <header className="bar">
        <span className="brand">dowel</span>
        <span className="mono muted">
          {run.head} · {run.files_changed} {run.files_changed === 1 ? "file" : "files"} against {run.base}
        </span>
        <nav className="mono muted nav">
          <a href="/" aria-current="page">
            this change
          </a>
          <a href="/decisions">decisions</a>
        </nav>
      </header>

      <main className="page">
        <div className="head">
          <h1>
            {shown.length === 0
              ? "Nothing here needs you."
              : unanswered > 0
                ? `${unanswered === 1 ? "One thing" : `${unanswered} things`} only you can decide.`
                : "Here's what we covered. Untick anything you'd rather keep to yourself."}
          </h1>
          <div className="stats mono muted">
            <span>
              <b>{checked}</b> ideas checked
            </span>
            <span>
              <b>{run.dropped.length}</b> dropped
            </span>
            <span className="hot">
              <b>{shown.length}</b> worth your time
            </span>
          </div>
        </div>

        {changeMap && changeMap.files.length > 0 && <Map change={changeMap} />}

        <div className="columns">
          <section className="items" aria-label="What Dowel found">
            {decisions.map(render)}
            {issues.map(render)}

            {background.length > 0 && (
              <details className="mono muted">
                <summary>
                  {background.length} smaller {background.length === 1 ? "thing" : "things"} Dowel chose not to raise
                </summary>
                <div className="dropped">
                  {background.map((f) => (
                    <div key={f.id}>{f.title}</div>
                  ))}
                </div>
              </details>
            )}

            {run.dropped.length > 0 && (
              <details className="mono muted">
                <summary>
                  {run.dropped.length} {run.dropped.length === 1 ? "idea" : "ideas"} Dowel checked and dropped
                </summary>
                <div className="dropped">
                  {run.dropped.map((d) => (
                    <div key={d.hypothesis.id}>
                      {d.hypothesis.statement}
                      <br />· {d.reason}
                    </div>
                  ))}
                </div>
              </details>
            )}
          </section>

          <aside className="aside" aria-label="Take it with you">
            {run.intent.summary && (
              <div className="card">
                <span className="mono muted">what this change does</span>
                <p>{run.intent.summary}</p>
              </div>
            )}

            {(why.decided.length > 0 || why.open.length > 0) && (
              <div className="card">
                <span className="mono eyebrow">why note</span>
                {why.decided.map((line) => (
                  <p key={line} className="lead">
                    {line}
                  </p>
                ))}
                {why.open.length > 0 && <span className="mono muted">still open</span>}
                {why.open.map((line) => (
                  <p key={line} className="muted">
                    {line}
                  </p>
                ))}
              </div>
            )}

            <div className="actions">
              {unanswered > 0 && (
                <a className="btn aslink" href="/talk">
                  Talk it through with Dowel
                </a>
              )}
              <CopyButton kind="brief" label="Copy the brief for the reviewer" primary />
              <CopyButton kind="summary" label={`Copy the review summary (${sharing})`} />
              <CopyButton kind="prompt" label="Copy fixes as a prompt for your agent" />
            </div>
            <p className="mono muted">
              Nothing leaves this page unless you copy it. Reviewed in {Math.round(run.seconds)} seconds.
            </p>
          </aside>
        </div>
      </main>
    </>
  );
}
