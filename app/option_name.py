from __future__ import annotations

import re
from typing import Any


def parse_option_name(text: str) -> dict[str, Any]:
    """
    Parse names like:
      WIPRO call 160
      wipro put 155
      WIPRO CE 162.5
      WIPRO PE 160
    """
    raw = " ".join((text or "").strip().split())
    if not raw:
        raise ValueError("Enter option name like: WIPRO call 160")

    pattern = re.compile(
        r"^(?P<stock>[A-Za-z][A-Za-z0-9.&-]*)\s+"
        r"(?P<opt>call|put|ce|pe)\s+"
        r"(?P<strike>\d+(?:\.\d+)?)\s*$",
        re.IGNORECASE,
    )
    m = pattern.match(raw)
    if not m:
        raise ValueError("Use format: STOCK call/put STRIKE  (example: WIPRO call 160)")

    opt = m.group("opt").upper()
    if opt in {"CALL", "CE"}:
        option_type = "CE"
    else:
        option_type = "PE"

    return {
        "stock": m.group("stock").upper(),
        "option_type": option_type,
        "strike": float(m.group("strike")),
        "display": f"{m.group('stock').upper()} {'call' if option_type == 'CE' else 'put'} {m.group('strike')}",
    }
