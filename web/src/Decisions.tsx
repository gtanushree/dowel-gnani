import { useEffect, useState } from "react";

type Row = {
  id: number;
  title: string;
  reason: string;
  stance: "keep" | "change" | "unsure";
  given: string;
  day: string;
  file: string;
  symbol: string;
  line: number;
  state: "fresh" | "drifted" | "orphaned";
  moved: string;
  shared: boolean;
  guarded: boolean;
  held_back: boolean;
};

const STATE: Record<Row["state"], string> = {
  fresh: "code unchanged",
  drifted: "code changed since",
  orphaned: "code not found",
};
const STANCE: Record<Row["stance"], string> = { keep: "Keep", change: "Change", unsure: "Not sure" };

// Every decision on record for the repository, grouped by file: what holds, what has drifted,
// and what a test now enforces.
export function Decisions() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [clashes, setClashes] = useState<number[][]>([]);
  const [only, setOnly] = useState<"all" | "drifted" | "guarded" | "spoken">("all");

  useEffect(() => {
    fetch("/api/decisions")
      .then((r) => (r.ok ? r.json() : { decisions: [], conflicts: [] }))
      .then((d) => {
        setRows(d.decisions);
        setClashes(d.conflicts);
      });
  }, []);

  if (!rows) return null;
  const shown = rows.filter((r) =>
    only === "drifted" ? r.state !== "fresh" : only === "guarded" ? r.guarded : only === "spoken" ? r.given === "spoken" : true,
  );
  const files = [...new Set(shown.map((r) => r.file))];
  const count = (test: (r: Row) => boolean) => rows.filter(test).length;
  const inClash = new Set(clashes.flat());

  return (
    <>
      <header className="bar">
        <span className="brand">dowel</span>
        <nav className="mono muted nav">
          <a href="/">this change</a>
          <a href="/decisions" aria-current="page">
            decisions
          </a>
        </nav>
        <span className="mono muted">only you can see this</span>
      </header>
      <main className="page">
        <div className="head">
          <h1>{rows.length === 0 ? "No decisions on record yet." : `${rows.length} decisions hold this code in place.`}</h1>
          <div className="stats mono muted">
            <span>
              <b>{count((r) => r.guarded)}</b> guarded by a test
            </span>
            <span>
              <b>{count((r) => r.given === "spoken")}</b> spoken
            </span>
            <span className={count((r) => r.state !== "fresh") > 0 ? "hot" : ""}>
              <b>{count((r) => r.state !== "fresh")}</b> need a look
            </span>
          </div>
        </div>

        {rows.length > 0 && (
          <div className="choices" role="group" aria-label="Show">
            {(["all", "drifted", "guarded", "spoken"] as const).map((key) => (
              <button key={key} type="button" className="choice" aria-pressed={only === key} onClick={() => setOnly(key)}>
                {key === "all" ? "All" : key === "drifted" ? "Need a look" : key === "guarded" ? "Guarded" : "Spoken"}
              </button>
            ))}
          </div>
        )}

        <div className="ledger">
          {files.map((file) => (
            <section key={file} className="group">
              <span className="mono where">{file}</span>
              {shown
                .filter((r) => r.file === file)
                .map((r) => (
                  <article key={r.id} className={`decision ${r.state}`}>
                    <div className="row">
                      <span className={`mono tag ${r.stance}`}>{STANCE[r.stance]}</span>
                      <span className="title">{r.title}</span>
                    </div>
                    {r.reason && <p className="said">"{r.reason}"</p>}
                    <div className="row mono muted">
                      <span>{r.symbol || "whole file"}</span>
                      <span>{r.day}</span>
                      {r.given && <span>{r.given}</span>}
                      <span className={r.state === "fresh" ? "" : "warn"}>{STATE[r.state]}</span>
                      {r.moved && <span>followed a {r.moved === "moved" ? "move" : "rename"}</span>}
                      {r.guarded && <span className="good">guarded by a test</span>}
                      {!r.shared && <span>only on this machine</span>}
                      {r.held_back && <span className="warn">held back from agents</span>}
                      {inClash.has(r.id) && <span className="warn">disagrees with another decision</span>}
                    </div>
                  </article>
                ))}
            </section>
          ))}
        </div>
      </main>
    </>
  );
}
