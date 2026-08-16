"""Circuit IR + deterministic fingerprints (spec S2).

The IR is deliberately thin: a netlist template with `{param}` placeholders plus a
param dict. That is enough to (a) render a byte-exact netlist, (b) fingerprint
topology separately from values, and (c) reproduce any candidate exactly.

ponytail: no component-graph object model. Add one only if a tool needs to
*rewrite* topology programmatically; today topology changes arrive as new templates.
"""

from __future__ import annotations

import hashlib
import re

PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)\}")

# A SPICE numeric literal, with optional engineering suffix (10k, 4.7u, 1e-9, 2Meg).
_NUM = re.compile(
    r"(?<![\w.])[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?(?:meg|mil|[tgkmunpf])?(?![\w.])",
    re.IGNORECASE,
)


def fmt_value(v) -> str:
    """Render a param for a netlist. Plain scientific notation -- SPICE accepts it,
    and it round-trips exactly, unlike engineering suffixes."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, int):
        return str(v)
    return repr(float(v)) if abs(v) < 1e16 else f"{v:.12g}"


# SPICE engineering suffixes, as powers of ten. Two traps, both silent and both off by
# 10^9:
#   * `m` is milli and `meg` is mega, and the whole thing is case-insensitive -- so `1M`
#     is one milliohm, not one megohm. Order matters below: `meg` must be tried before
#     `m`, or `1meg` parses as 1e-3 and takes an `eg` suffix with it.
#   * SPICE ignores trailing text, so `1kohm` is 1000 and `4.7uF` is 4.7e-6.
_SUFFIX = [("meg", 6), ("t", 12), ("g", 9), ("k", 3), ("m", -3), ("u", -6), ("µ", -6),
           ("n", -9), ("p", -12), ("f", -15)]
_LEAD_NUM = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(.*)$")


def parse_value(tok: str) -> float | None:
    """`4.7u` -> 4.7e-6. None if `tok` is not a numeric literal at all.

    None rather than a raise: a netlist token is often `{R1}` or a model name, and the
    callers here are scanning element lines where "not a number" is the normal case.

    The exponent is applied by re-parsing `<mantissa>e<exp>` rather than by multiplying,
    because `100 * 1e-9` is 1.0000000000000001e-07 while `100e-9` is exactly 1e-07 --
    and the difference ends up written into netlists and fingerprints.
    """
    m = _LEAD_NUM.match(str(tok))
    if not m:
        return None
    mant, rest = m.group(1), m.group(2).strip().lower()
    if rest.startswith("mil"):
        return float(mant) * 25.4e-6
    for suf, exp in _SUFFIX:
        if rest.startswith(suf):
            if "e" in mant or "E" in mant:              # `1e3k`: no clean literal to build
                return float(mant) * 10.0 ** exp
            return float(f"{mant}e{exp}")
    return float(mant)


def render(template: str, params: dict) -> tuple[str, list[str]]:
    """Substitute `{name}` placeholders -> (netlist, unfilled_placeholders).

    Unknown `{...}` (LTspice expressions) are left untouched, so behavioural
    sources keep working."""
    missing = {m.group(1) for m in PLACEHOLDER.finditer(template)} - set(params)

    def sub(m: re.Match) -> str:
        name = m.group(1)
        return fmt_value(params[name]) if name in params else m.group(0)

    netlist = PLACEHOLDER.sub(sub, template)
    return netlist, sorted(missing)


def normalize(netlist: str) -> str:
    """Canonical form for hashing: drop comments/blanks, collapse whitespace, fold case.

    `.model` / `.include` / `.lib` lines are kept -- component models are part of
    identity per spec S2.
    """
    out = []
    for line in netlist.splitlines():
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        line = re.sub(r"\s*;.*$", "", line)  # trailing SPICE comment
        line = re.sub(r"\s+", " ", line).lower()
        if line:
            out.append(line)
    # `.end` position is not semantic; everything else keeps source order.
    return "\n".join(out)


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x00")
    return h.hexdigest()[:32]


def topology_fp(netlist: str) -> str:
    """Structure without values: every numeric literal becomes `?`.

    Two circuits with the same wiring and different R/C values share this hash.
    """
    return _sha("topo", _NUM.sub("?", normalize(netlist)))


def design_fp(netlist: str, params: dict) -> str:
    """Full design identity: topology + values + models + directives."""
    canon = normalize(netlist)
    pstr = ";".join(f"{k}={fmt_value(params[k])}" for k in sorted(params))
    return _sha("design", canon, pstr)


def config_fp(sim_config: dict) -> str:
    """Simulation conditions. Spec S13: same circuit + different conditions is a
    different *experiment*, not a duplicate."""
    def canon(o):
        if isinstance(o, dict):
            return {k: canon(o[k]) for k in sorted(o)}
        if isinstance(o, (list, tuple)):
            return [canon(x) for x in o]
        return o

    import json

    return _sha("cfg", json.dumps(canon(sim_config), separators=(",", ":")))


def experiment_fp(dfp: str, cfp: str) -> str:
    return _sha("exp", dfp, cfp)
