// A small line-at-a-time highlighter for the code panel. It colours keywords, strings, numbers
// and comments, which is all the walkthrough needs, without shipping a grammar per language.

export type Token = { kind: "" | "kw" | "str" | "num" | "com"; text: string };

const WORDS: Record<string, string> = {
  py: "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case self",
  js: "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof interface let new null of return static super switch this throw true try type typeof undefined var void while yield enum implements readonly as",
  go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
  rs: "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while",
  c: "auto bool break case catch char class const constexpr continue default delete do double else enum explicit extern false float for friend if inline int long namespace new nullptr operator private protected public return short signed sizeof static struct switch template this throw true try typedef typename union unsigned using virtual void volatile while NULL",
  java: "abstract boolean break byte case catch char class const continue default do double else enum extends false final finally float for if implements import instanceof int interface long new null package private protected public return short static super switch this throw throws true try void volatile while var",
};

const FAMILY: Record<string, keyof typeof WORDS> = {
  py: "py", js: "js", jsx: "js", ts: "js", tsx: "js", mjs: "js", cjs: "js", go: "go", rs: "rs",
  c: "c", h: "c", cc: "c", cpp: "c", hpp: "c", cs: "java", java: "java", kt: "java", swift: "java",
};

const HASH_COMMENTS = new Set(["py", "rb", "sh", "bash", "yml", "yaml", "toml"]);
const cache = new Map<string, { words: Set<string>; pattern: RegExp }>();

function rules(file: string) {
  const ext = file.split(".").pop()?.toLowerCase() ?? "";
  const known = cache.get(ext);
  if (known) return known;
  const family = FAMILY[ext];
  const comment = HASH_COMMENTS.has(ext) ? "#.*" : "\\/\\/.*|\\/\\*.*?(?:\\*\\/|$)";
  // Rust uses a single quote for lifetimes, so only double quotes count as strings there.
  const single = family === "rs" ? "" : "|'(?:\\\\.|[^'\\\\])*'";
  const made = {
    words: new Set((family ? WORDS[family] : "").split(" ").filter(Boolean)),
    pattern: new RegExp(
      `(${comment})|("(?:\\\\.|[^"\\\\])*"${single}|\`(?:\\\\.|[^\`\\\\])*\`)|(\\b\\d[\\w.]*\\b)|([A-Za-z_][A-Za-z0-9_]*)`,
      "g",
    ),
  };
  cache.set(ext, made);
  return made;
}

export function highlight(file: string, line: string): Token[] {
  const { words, pattern } = rules(file);
  const out: Token[] = [];
  let at = 0;
  pattern.lastIndex = 0;
  for (let m = pattern.exec(line); m; m = pattern.exec(line)) {
    if (m[0] === "") {
      pattern.lastIndex += 1;
      continue;
    }
    const kind: Token["kind"] = m[1] ? "com" : m[2] ? "str" : m[3] ? "num" : words.has(m[4]) ? "kw" : "";
    if (kind === "") continue;
    if (m.index > at) out.push({ kind: "", text: line.slice(at, m.index) });
    out.push({ kind, text: m[0] });
    at = m.index + m[0].length;
  }
  if (at < line.length) out.push({ kind: "", text: line.slice(at) });
  return out;
}
