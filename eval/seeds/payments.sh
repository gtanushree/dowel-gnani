#!/usr/bin/env bash
# Builds the seeded "payments" repository: main, a retry-webhooks branch with planted problems, and a later
# harden-loader branch that quietly reverses one behaviour (to show memory). Usage: payments.sh <target-dir>   (the directory is wiped and recreated)
set -euo pipefail
D="${1:?target directory}"
rm -rf "$D"; mkdir -p "$D"/{config,webhooks,jobs}; cd "$D"; git init -q -b main
commit() { git add -A; git -c user.name=demo -c user.email=demo@example.com commit -q -m "$1"; }

cat > config/loader.py <<'EOF'
"""Loads service configuration from a TOML file."""
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class ConfigMissing(Exception):
    pass


@dataclass
class Config:
    webhook_timeout_s: float
    verify_signature: bool
    signing_secret: str = ""


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigMissing(path)
    raw = tomllib.loads(path.read_text())
    return Config(**raw["webhooks"])
EOF
cat > webhooks/signing.py <<'EOF'
"""HMAC signing for outgoing webhook payloads."""
import hashlib
import hmac


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
EOF
cat > webhooks/deliver.py <<'EOF'
"""Delivers webhook events to customer endpoints."""
import json
import logging
from dataclasses import dataclass

import httpx

from config.loader import Config
from webhooks.signing import sign

log = logging.getLogger(__name__)


@dataclass
class Delivery:
    id: str
    url: str
    payload: dict
    status: str = "pending"
    attempts: int = 0


def deliver(delivery: Delivery, config: Config) -> None:
    body = json.dumps(delivery.payload).encode()
    headers = {"content-type": "application/json"}
    if config.verify_signature:
        headers["x-signature"] = sign(config.signing_secret, body)
    delivery.attempts += 1
    response = httpx.post(delivery.url, content=body, headers=headers, timeout=config.webhook_timeout_s)
    response.raise_for_status()
    delivery.status = "delivered"
EOF
cat > app.py <<'EOF'
"""Service entry point."""
from pathlib import Path

from config.loader import load_config

CONFIG_PATH = Path("/etc/payments/config.toml")


def main() -> None:
    config = load_config(CONFIG_PATH)
    print(f"payments service up, signature checking: {config.verify_signature}")


if __name__ == "__main__":
    main()
EOF
cat > jobs/redeliver.py <<'EOF'
"""Nightly job that re-sends deliveries stuck in pending."""
from pathlib import Path

from config.loader import ConfigMissing, load_config
from webhooks.deliver import Delivery, deliver


def run(pending: list[Delivery], config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except ConfigMissing:
        # No config on this host means webhooks are not enabled here: do nothing.
        return 0
    sent = 0
    for delivery in pending:
        deliver(delivery, config)
        sent += 1
    return sent
EOF
touch config/__init__.py webhooks/__init__.py jobs/__init__.py
commit "Payments webhook service"

git switch -q -c retry-webhooks
cat > webhooks/backoff.py <<'EOF'
"""Delay between webhook retries."""


def next_delay(base_delay_s: float, attempt: int) -> float:
    return base_delay_s * 2 ** attempt
EOF
python3 - <<'EOF'
from pathlib import Path

p = Path("config/loader.py"); s = p.read_text()
s = s.replace('''@dataclass
class Config:
    webhook_timeout_s: float
    verify_signature: bool
    signing_secret: str = ""
''', '''DEFAULTS = {
    "webhook_timeout_s": 5.0,
    "max_retries": 3,
    "base_delay_s": 2.0,
    "verify_signature": False,
}


@dataclass
class Config:
    webhook_timeout_s: float
    verify_signature: bool
    signing_secret: str = ""
    max_retries: int = 3
    base_delay_s: float = 2.0
''')
s = s.replace('''    if not path.exists():
        raise ConfigMissing(path)
''', '''    if not path.exists():
        log.warning("config missing, using defaults")
        return Config(**DEFAULTS)
''')
p.write_text(s)

p = Path("webhooks/deliver.py"); s = p.read_text()
s = s.replace("import logging\n", "import logging\nimport time\n")
s = s.replace("from webhooks.signing import sign", "from webhooks.backoff import next_delay\nfrom webhooks.signing import sign")
s = s.replace('''    delivery.attempts += 1
    response = httpx.post(delivery.url, content=body, headers=headers, timeout=config.webhook_timeout_s)
    response.raise_for_status()
    delivery.status = "delivered"
''', '''    for attempt in range(config.max_retries):
        delivery.attempts += 1
        try:
            response = httpx.post(delivery.url, content=body, headers=headers, timeout=config.webhook_timeout_s)
            response.raise_for_status()
            delivery.status = "delivered"
            return
        except httpx.HTTPError as exc:
            log.warning("delivery %s attempt %d failed: %s", delivery.id, attempt + 1, exc)
            time.sleep(next_delay(config.base_delay_s, attempt))
    delivery.status = "failed"
''')
p.write_text(s)
EOF
commit "Retry failed webhooks with backoff

Failed deliveries are retried up to three times with exponential backoff."

# A later change, as a coding agent might make it: it validates the new settings, and while there it
# "tidies" the loader so a missing config file stops the app again. Reviewed against retry-webhooks:
#   git switch harden-loader && dowel --base retry-webhooks
git switch -q -c harden-loader
python3 - <<'EOF'
from pathlib import Path

p = Path("config/loader.py"); s = p.read_text()
s = s.replace('''    if not path.exists():
        log.warning("config missing, using defaults")
        return Config(**DEFAULTS)
''', '''    if not path.exists():
        raise ConfigMissing(path)
''')
s = s.replace('''    return Config(**raw["webhooks"])
''', '''    config = Config(**{**DEFAULTS, **raw["webhooks"]})
    if config.max_retries < 1:
        raise ValueError("max_retries must be at least 1")
    return config
''')
p.write_text(s)
EOF
commit "Validate webhook settings

max_retries must be at least 1, and settings missing from the file take their defaults."
git switch -q retry-webhooks
