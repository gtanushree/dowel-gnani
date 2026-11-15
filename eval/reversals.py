"""How often is a change that undoes a decision caught, and how often is a harmless one flagged?

Each case is a small function with a recorded decision to keep some behaviour, and an edit to
that function. Some edits undo the decision; others change the function without undoing it.
Every case goes through the real path: the decision is recorded as a file, the edit is applied,
and `dowel check --judge` decides.

    uv run python eval/reversals.py

Ten scenarios, two undoing edits and three harmless edits each: 20 and 30 cases. The cases were
written by a model, not collected from real repositories, and the functions are short. Edits that
only touch comments or layout never reach a model: the fingerprint already says nothing changed.
Each judged case is one small model call, about a cent.
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scenario:
    name: str
    function: str  # the function as first written
    decision: str
    reason: str
    undo: tuple[str, str]  # two edits that go against the decision
    harmless: tuple[str, str, str]  # three edits that do not


S = Scenario
SCENARIOS = [
    S("config_fallback",
      "def load_config(path):\n    if not path.exists():\n        return dict(DEFAULTS)\n    return parse(path.read_text())\n",
      "A missing config file falls back to defaults instead of raising.", "Staging hosts have no config file.",
      ("def load_config(path):\n    if not path.exists():\n        raise FileNotFoundError(path)\n    return parse(path.read_text())\n",
       "def load_config(path):\n    return parse(path.read_text())\n"),
      ("def load_config(path):\n    # staging has no file\n    if not path.exists():\n        return dict(DEFAULTS)\n    return parse(path.read_text())\n",
       "def load_config(path):\n    if not path.exists():\n        log.info('no config at %s, using defaults', path)\n        return dict(DEFAULTS)\n    return parse(path.read_text())\n",
       "def load_config(path):\n    if not path.exists():\n        return dict(DEFAULTS)\n    text = path.read_text(encoding='utf-8')\n    return parse(text)\n")),
    S("charge_once",
      "def charge(order):\n    if order.id in charged:\n        return charged[order.id]\n    receipt = gateway.charge(order.total)\n    charged[order.id] = receipt\n    return receipt\n",
      "An order that was already charged returns the earlier receipt and is not charged again.", "Double charges cost us refunds.",
      ("def charge(order):\n    receipt = gateway.charge(order.total)\n    charged[order.id] = receipt\n    return receipt\n",
       "def charge(order):\n    if order.id in charged and not order.retry:\n        return charged[order.id]\n    receipt = gateway.charge(order.total)\n    charged[order.id] = receipt\n    return receipt\n"),
      ("def charge(order):\n    if order.id in charged:\n        return charged[order.id]\n    receipt = gateway.charge(order.total)\n    charged[order.id] = receipt\n    metrics.count('charges')\n    return receipt\n",
       "def charge(order):\n    previous = charged.get(order.id)\n    if previous is not None:\n        return previous\n    receipt = gateway.charge(order.total)\n    charged[order.id] = receipt\n    return receipt\n",
       "def charge(order):\n    if order.id in charged:\n        return charged[order.id]\n    receipt = gateway.charge(order.total, currency=order.currency)\n    charged[order.id] = receipt\n    return receipt\n")),
    S("soft_delete",
      "def delete_user(user):\n    user.deleted_at = now()\n    user.save()\n",
      "Deleting a user marks them deleted and keeps the row.", "Finance needs the records for seven years.",
      ("def delete_user(user):\n    user.delete()\n",
       "def delete_user(user):\n    db.execute('DELETE FROM users WHERE id = ?', (user.id,))\n"),
      ("def delete_user(user):\n    user.deleted_at = now()\n    user.save()\n    audit.record('user_deleted', user.id)\n",
       "def delete_user(user):\n    when = now()\n    user.deleted_at = when\n    user.save()\n",
       "def delete_user(user):\n    user.deleted_at = now()\n    user.email = None\n    user.save()\n")),
    S("signature_required",
      "def handle_webhook(request):\n    if not valid_signature(request):\n        return reject(401)\n    return process(request.json)\n",
      "A webhook without a valid signature is rejected.", "Anyone can find the endpoint.",
      ("def handle_webhook(request):\n    return process(request.json)\n",
       "def handle_webhook(request):\n    if settings.verify and not valid_signature(request):\n        return reject(401)\n    return process(request.json)\n"),
      ("def handle_webhook(request):\n    if not valid_signature(request):\n        log.warning('bad signature from %s', request.ip)\n        return reject(401)\n    return process(request.json)\n",
       "def handle_webhook(request):\n    if not valid_signature(request):\n        return reject(401)\n    payload = request.json\n    return process(payload)\n",
       "def handle_webhook(request):\n    if not valid_signature(request):\n        return reject(401)\n    if request.json.get('test'):\n        return ok()\n    return process(request.json)\n")),
    S("retry_limit",
      "def send(message):\n    for attempt in range(3):\n        if deliver(message):\n            return True\n        sleep(2 ** attempt)\n    return False\n",
      "Sending gives up after three attempts.", "The provider bans senders that hammer it.",
      ("def send(message):\n    attempt = 0\n    while not deliver(message):\n        sleep(2 ** attempt)\n        attempt += 1\n    return True\n",
       "def send(message):\n    for attempt in range(50):\n        if deliver(message):\n            return True\n        sleep(2 ** attempt)\n    return False\n"),
      ("def send(message):\n    for attempt in range(3):\n        if deliver(message):\n            return True\n        sleep(min(2 ** attempt, 30))\n    return False\n",
       "def send(message):\n    for attempt in range(3):\n        if deliver(message):\n            return True\n        log.info('attempt %d failed', attempt + 1)\n        sleep(2 ** attempt)\n    return False\n",
       "MAX_ATTEMPTS = 3\n\n\ndef send(message):\n    for attempt in range(MAX_ATTEMPTS):\n        if deliver(message):\n            return True\n        sleep(2 ** attempt)\n    return False\n")),
    S("round_half_even",
      "def to_cents(amount):\n    return int(Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN) * 100)\n",
      "Amounts are rounded half to even.", "The ledger and the bank must agree to the cent.",
      ("def to_cents(amount):\n    return int(round(float(amount) * 100))\n",
       "def to_cents(amount):\n    return int(Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) * 100)\n"),
      ("def to_cents(amount):\n    rounded = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)\n    return int(rounded * 100)\n",
       "def to_cents(amount):\n    if amount is None:\n        raise ValueError('amount is required')\n    return int(Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN) * 100)\n",
       "CENT = Decimal('0.01')\n\n\ndef to_cents(amount):\n    return int(Decimal(amount).quantize(CENT, rounding=ROUND_HALF_EVEN) * 100)\n")),
    S("cache_bypass_for_admin",
      "def get_report(user, key):\n    if user.is_admin:\n        return build_report(key)\n    return cache.get_or_set(key, lambda: build_report(key))\n",
      "Admins always get a freshly built report, never a cached one.", "Support reads these while fixing data and must see the fix.",
      ("def get_report(user, key):\n    return cache.get_or_set(key, lambda: build_report(key))\n",
       "def get_report(user, key):\n    if user.is_admin and user.wants_fresh:\n        return build_report(key)\n    return cache.get_or_set(key, lambda: build_report(key))\n"),
      ("def get_report(user, key):\n    if user.is_admin:\n        return build_report(key)\n    return cache.get_or_set(key, lambda: build_report(key), ttl=300)\n",
       "def get_report(user, key):\n    if user.is_admin:\n        report = build_report(key)\n        cache.set(key, report)\n        return report\n    return cache.get_or_set(key, lambda: build_report(key))\n",
       "def get_report(user, key):\n    build = lambda: build_report(key)\n    if user.is_admin:\n        return build()\n    return cache.get_or_set(key, build)\n")),
    S("empty_cart_allowed",
      "def checkout(cart):\n    if not cart.items:\n        return Order.empty()\n    return Order.create(cart.items)\n",
      "Checking out an empty cart returns an empty order instead of an error.", "The mobile app calls checkout to refresh totals.",
      ("def checkout(cart):\n    if not cart.items:\n        raise ValueError('cart is empty')\n    return Order.create(cart.items)\n",
       "def checkout(cart):\n    assert cart.items, 'cart is empty'\n    return Order.create(cart.items)\n"),
      ("def checkout(cart):\n    if not cart.items:\n        return Order.empty()\n    return Order.create(cart.items, coupon=cart.coupon)\n",
       "def checkout(cart):\n    items = cart.items\n    if not items:\n        return Order.empty()\n    return Order.create(items)\n",
       "def checkout(cart):\n    if not cart.items:\n        metrics.count('empty_checkout')\n        return Order.empty()\n    return Order.create(cart.items)\n")),
    S("timeout_five_seconds",
      "def fetch_rates(client):\n    try:\n        return client.get('/rates', timeout=5).json()\n    except Timeout:\n        return LAST_KNOWN_RATES\n",
      "If rates do not arrive in five seconds, the last known rates are used.", "Checkout must not hang on the rates service.",
      ("def fetch_rates(client):\n    return client.get('/rates', timeout=5).json()\n",
       "def fetch_rates(client):\n    try:\n        return client.get('/rates').json()\n    except Timeout:\n        return LAST_KNOWN_RATES\n"),
      ("def fetch_rates(client):\n    try:\n        return client.get('/rates', timeout=5).json()\n    except Timeout:\n        log.warning('rates timed out, using last known')\n        return LAST_KNOWN_RATES\n",
       "def fetch_rates(client):\n    try:\n        response = client.get('/rates', timeout=5)\n        return response.json()\n    except Timeout:\n        return LAST_KNOWN_RATES\n",
       "def fetch_rates(client):\n    try:\n        return client.get('/rates', timeout=5, headers=AUTH).json()\n    except Timeout:\n        return LAST_KNOWN_RATES\n")),
    S("lowercase_emails",
      "def normalise_email(email):\n    return email.strip().lower()\n",
      "Email addresses are compared in lower case.", "People sign up as Anna@ and log in as anna@.",
      ("def normalise_email(email):\n    return email.strip()\n",
       "def normalise_email(email):\n    local, _, domain = email.strip().partition('@')\n    return local + '@' + domain.lower()\n"),
      ("def normalise_email(email):\n    return email.strip().lower().replace(' ', '')\n",
       "def normalise_email(email):\n    cleaned = email.strip()\n    return cleaned.lower()\n",
       "def normalise_email(email):\n    if email is None:\n        return ''\n    return email.strip().lower()\n")),
]


def wilson(hits: int, n: int) -> tuple[float, float]:
    """A 95% interval for a proportion that behaves sensibly for small counts."""
    if n == 0:
        return 0.0, 1.0
    z, p = 1.96, hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=eval", "-c", "user.email=eval@example.invalid", *args],
                   check=True, capture_output=True)


async def one(scenario: Scenario, kind: str, index: int, edited: str, into: Path, gate: asyncio.Semaphore) -> dict:
    from dowel import check
    from dowel.memory import Memory
    from dowel.schemas import Evidence, Finding, Hypothesis, Verdict
    from dowel.store import ItemState

    repo = into / f"{scenario.name}-{kind}-{index}"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "service.py").write_text(scenario.function)
    git(repo, "add", "."), git(repo, "commit", "-qm", "start")
    line = next(n for n, text in enumerate(scenario.function.splitlines(), 1) if text.startswith("def ")) + 1
    hypothesis = Hypothesis(id="h1", statement=scenario.decision, bug_class="other", file="service.py", line=line, check="-")
    verdict = Verdict(status="confirmed", reasoning="-", trigger="-", question_for_author="",
                      evidence=[Evidence(file="service.py", start_line=line, end_line=line, quote="-")])
    finding = Finding(id="h1", kind="decision", title=scenario.decision, file="service.py", line=line,
                      hypothesis=hypothesis, verdict=verdict)
    memory = Memory(repo)
    memory.remember_answer(finding, ItemState(answer="keep", note=scenario.reason), given="typed")
    memory.close()
    git(repo, "add", "."), git(repo, "commit", "-qm", "record the decision")
    git(repo, "switch", "-qc", "change")
    (repo / "service.py").write_text(edited)

    base = subprocess.run(["git", "-C", str(repo), "merge-base", "main", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    report = check.gather(repo, base)
    judged = any(p.kind == "decision" for p in report.problems)
    if judged:
        async with gate:
            await check.judge(repo, base, report)
    flagged = any(p.kind == "decision" for p in report.problems)
    return {"scenario": scenario.name, "kind": kind, "index": index, "flagged": flagged, "asked_model": judged,
            "said": next((p.what[:200] for p in report.problems if p.kind == "decision"), "")}


async def main() -> None:
    into = Path(tempfile.mkdtemp(prefix="dowel-reversals-"))
    Path.home = lambda: into / "home"  # type: ignore[method-assign]  # keep the index for these throwaway repos out of ~
    gate = asyncio.Semaphore(6)
    jobs = [one(s, kind, i, text, into, gate)
            for s in SCENARIOS for kind, edits in (("undo", s.undo), ("harmless", s.harmless)) for i, text in enumerate(edits)]
    results = await asyncio.gather(*jobs)

    undo = [r for r in results if r["kind"] == "undo"]
    harmless = [r for r in results if r["kind"] == "harmless"]
    caught, alarms = sum(r["flagged"] for r in undo), sum(r["flagged"] for r in harmless)
    lo, hi = wilson(caught, len(undo))
    alo, ahi = wilson(alarms, len(harmless))
    print(f"edits that undo the decision:  caught {caught} of {len(undo)}   ({lo:.0%} to {hi:.0%})")
    print(f"edits that do not:             flagged {alarms} of {len(harmless)}   ({alo:.0%} to {ahi:.0%})")
    print(f"model calls: {sum(r['asked_model'] for r in results)} of {len(results)} cases")
    for r in results:
        wrong = r["flagged"] != (r["kind"] == "undo")
        if wrong:
            print(f"  wrong: {r['scenario']} {r['kind']} {r['index']}: {r['said'] or 'not flagged'}")
    saved = Path(__file__).parent / "results" / f"reversals-{time.strftime('%Y%m%d-%H%M%S')}.json"
    saved.parent.mkdir(exist_ok=True)
    saved.write_text(json.dumps(results, indent=2))
    print(f"details: {saved}")


if __name__ == "__main__":
    asyncio.run(main())
