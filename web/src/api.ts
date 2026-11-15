export type Kind = "issue" | "decision" | "background";
export type Answer = "keep" | "change" | "unsure";

export interface Evidence {
  file: string;
  start_line: number;
  end_line: number;
  quote: string;
}

export interface Hypothesis {
  id: string;
  statement: string;
  file: string;
  line: number;
}

export interface Finding {
  id: string;
  kind: Kind;
  title: string;
  detail: string;
  question: string;
  memory: string;
  priority: string;
  file: string;
  line: number;
  hypothesis: Hypothesis;
  verdict: { reasoning: string; evidence: Evidence[] };
  review: { refutation_attempt: string } | null;
  searched: string[];
}

export interface Dropped {
  hypothesis: Hypothesis;
  reason: string;
  stage: string;
}

export interface ItemState {
  included: boolean;
  answer: Answer | null;
  note: string;
  noise: boolean;
}

export interface Sheet {
  run: {
    repo: string;
    base: string;
    head: string;
    files_changed: number;
    intent: { summary: string };
    findings: Finding[];
    dropped: Dropped[];
    seconds: number;
  };
  state: { items: Record<string, ItemState> };
  labels: Record<string, string>;
  why: { decided: string[]; open: string[] };
}

export interface ItemPatch {
  included?: boolean;
  answer?: Answer;
  clear_answer?: boolean;
  note?: string;
  noise?: boolean;
}

async function ok(response: Response): Promise<Response> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail ?? `Request failed (${response.status})`);
  }
  return response;
}

export async function getSheet(): Promise<Sheet> {
  return (await ok(await fetch("/api/run"))).json();
}

export async function patchItem(id: string, patch: ItemPatch): Promise<Sheet> {
  const response = await fetch(`/api/items/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
  return (await ok(response)).json();
}

export interface ChangedFile {
  path: string;
  label: "asked for" | "needed for it" | "mechanical" | "not explained";
  why: string;
  added: number;
  removed: number;
}

export interface ChangeMap {
  asked: string;
  source: string;
  files: ChangedFile[];
  problems: { kind: string; where: string; what: string; lines: string[] }[];
  notes: string[];
}

export async function getChange(): Promise<ChangeMap> {
  return (await ok(await fetch("/api/change"))).json();
}

export async function getExport(kind: "summary" | "prompt" | "brief"): Promise<string> {
  return (await ok(await fetch(`/api/export/${kind}`))).text();
}
