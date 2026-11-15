#!/usr/bin/env bash
# Builds the held-out "inventory" repository: main, a bulk-reserve branch with planted problems, and a
# tidy branch that changes nothing in behaviour. Written after the review prompts were frozen and
# not used to tune them. Usage: inventory.sh <target-dir>   (the directory is wiped and recreated)
set -euo pipefail
D="${1:?target directory}"
rm -rf "$D"; mkdir -p "$D"/{stock,jobs,tests}; cd "$D"; git init -q -b main
commit() { git add -A; git -c user.name=demo -c user.email=demo@example.com commit -q -m "$1"; }

cat > stock/store.py <<'EOF'
"""Stock levels and short-lived reservations made while a customer checks out."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

RESERVATION_MINUTES = 15


class OutOfStock(Exception):
    pass


@dataclass
class Reservation:
    sku: str
    quantity: int
    expires_at: datetime


@dataclass
class Store:
    on_hand: dict[str, int] = field(default_factory=dict)
    reservations: list[Reservation] = field(default_factory=list)

    def available(self, sku: str) -> int:
        held = sum(r.quantity for r in self.reservations if r.sku == sku)
        return self.on_hand.get(sku, 0) - held

    def reserve(self, sku: str, quantity: int, now: datetime) -> Reservation:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.available(sku) < quantity:
            raise OutOfStock(sku)
        reservation = Reservation(sku, quantity, now + timedelta(minutes=RESERVATION_MINUTES))
        self.reservations.append(reservation)
        return reservation

    def release(self, reservation: Reservation) -> None:
        self.reservations.remove(reservation)
EOF
cat > stock/api.py <<'EOF'
"""HTTP handlers. Each returns a plain dict that the framework turns into JSON."""
from datetime import datetime

from stock.store import OutOfStock, Store

store = Store()


def post_reserve(body: dict) -> dict:
    try:
        reservation = store.reserve(body["sku"], int(body["quantity"]), datetime.utcnow())
    except OutOfStock:
        return {"ok": False, "error": "out_of_stock"}
    return {"ok": True, "expires_at": reservation.expires_at.isoformat()}
EOF
cat > jobs/expire.py <<'EOF'
"""Runs every minute and frees reservations whose time is up."""
from datetime import datetime

from stock.api import store


def run() -> int:
    now = datetime.utcnow()
    expired = [r for r in store.reservations if r.expires_at <= now]
    for reservation in expired:
        store.release(reservation)
    return len(expired)
EOF
cat > tests/test_store.py <<'EOF'
from datetime import datetime

import pytest

from stock.store import OutOfStock, Store


def test_reserving_more_than_is_available_fails():
    store = Store(on_hand={"a": 2})
    store.reserve("a", 2, datetime(2026, 1, 1))
    with pytest.raises(OutOfStock):
        store.reserve("a", 1, datetime(2026, 1, 1))
EOF
touch stock/__init__.py jobs/__init__.py
commit "Stock levels with reservations"

git switch -q -c bulk-reserve
python3 - <<'EOF'
from pathlib import Path
p = Path("stock/store.py"); s = p.read_text()
s = s.replace("RESERVATION_MINUTES = 15", "RESERVATION_MINUTES = 60")
s += '''
    def reserve_many(self, lines: list[tuple[str, int]], now: datetime) -> list[Reservation]:
        """Reserve several items for one basket."""
        made = []
        for sku, quantity in lines:
            if self.available(sku) < quantity:
                raise OutOfStock(sku)
            reservation = Reservation(sku, quantity, now + timedelta(minutes=RESERVATION_MINUTES))
            self.reservations.append(reservation)
            made.append(reservation)
        return made
'''
p.write_text(s)
p = Path("stock/api.py"); s = p.read_text()
s += '''

def post_reserve_bulk(body: dict) -> dict:
    lines = [(line["sku"], int(line["quantity"])) for line in body["lines"]]
    try:
        made = store.reserve_many(lines, datetime.utcnow())
    except OutOfStock:
        made = []
    return {"ok": True, "reserved": len(made)}
'''
p.write_text(s)
EOF
commit "Add bulk reservation

A basket can now be reserved in one request through post_reserve_bulk."

git switch -q main; git switch -q -c tidy
python3 - <<'EOF'
from pathlib import Path
p = Path("stock/store.py"); s = p.read_text()
s = s.replace('''        held = sum(r.quantity for r in self.reservations if r.sku == sku)
        return self.on_hand.get(sku, 0) - held''', '''        return self.on_hand.get(sku, 0) - self._held(sku)

    def _held(self, sku: str) -> int:
        """How many units of this item are currently reserved."""
        return sum(r.quantity for r in self.reservations if r.sku == sku)''')
p.write_text(s)
p = Path("tests/test_store.py"); s = p.read_text()
s += '''

def test_releasing_a_reservation_frees_the_stock():
    store = Store(on_hand={"a": 2})
    reservation = store.reserve("a", 2, datetime(2026, 1, 1))
    store.release(reservation)
    assert store.available("a") == 2
'''
p.write_text(s)
EOF
commit "Extract the count of held units into a helper

No change in behaviour. Adds a test for releasing a reservation."
git switch -q bulk-reserve
