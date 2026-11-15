#!/usr/bin/env bash
# Builds the held-out "notifier" repository (TypeScript): main, and a daily-digest branch with planted
# problems. Written after the review prompts were frozen and not used to tune them.
# Usage: notifier.sh <target-dir>   (the directory is wiped and recreated)
set -euo pipefail
D="${1:?target directory}"
rm -rf "$D"; mkdir -p "$D"/src; cd "$D"; git init -q -b main
commit() { git add -A; git -c user.name=demo -c user.email=demo@example.com commit -q -m "$1"; }

cat > src/users.ts <<'EOF'
export interface User {
  id: string;
  email: string;
  timezone: string; // IANA name, such as "Asia/Kolkata"
  unsubscribed: boolean;
}

export interface UserStore {
  all(): Promise<User[]>;
}
EOF
cat > src/limiter.ts <<'EOF'
// At most MAX_PER_HOUR emails to one person in any hour, whatever part of the app sends them.
export const MAX_PER_HOUR = 3;

const sent = new Map<string, number[]>();

export function allowed(userId: string, now: number): boolean {
  const recent = (sent.get(userId) ?? []).filter((t) => now - t < 3_600_000);
  sent.set(userId, recent);
  return recent.length < MAX_PER_HOUR;
}

export function record(userId: string, now: number): void {
  sent.set(userId, [...(sent.get(userId) ?? []), now]);
}
EOF
cat > src/send.ts <<'EOF'
import { allowed, record } from "./limiter";
import type { User } from "./users";

export interface Provider {
  deliver(to: string, subject: string, body: string): Promise<void>;
}

// The one way the app sends email: respects unsubscribes and the hourly limit.
export async function send(provider: Provider, user: User, subject: string, body: string): Promise<boolean> {
  const now = Date.now();
  if (user.unsubscribed || !allowed(user.id, now)) {
    return false;
  }
  await provider.deliver(user.email, subject, body);
  record(user.id, now);
  return true;
}
EOF
commit "Email sending with unsubscribe and an hourly limit"

git switch -q -c daily-digest
python3 - <<'EOF'
from pathlib import Path
p = Path("src/limiter.ts"); p.write_text(p.read_text().replace("export const MAX_PER_HOUR = 3;", "export const MAX_PER_HOUR = 10;"))
Path("src/digest.ts").write_text('''import type { Provider } from "./send";
import type { UserStore } from "./users";

export interface Summary {
  subject: string;
  body: string;
}

// Sends each person their daily digest at 9am in their own time zone. Called once an hour.
export async function sendDigests(provider: Provider, users: UserStore, summarise: (id: string) => Promise<Summary>): Promise<number> {
  if (new Date().getHours() !== 9) {
    return 0;
  }
  const everyone = (await users.all()).slice(0, 100);
  let count = 0;
  for (const user of everyone) {
    const summary = await summarise(user.id);
    await provider.deliver(user.email, summary.subject, summary.body);
    count += 1;
  }
  return count;
}
''')
EOF
commit "Add daily digest emails

Everyone gets a digest of their activity at 9am their time."
