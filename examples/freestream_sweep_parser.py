"""Freestream run-sheet sweep-cell parser (reference implementation).
Grammar (see RunSheet Guide tab):
  single | comma-list | a:d:b (start:delta:end) | ...R (return) | mix | @named | csv:file
Mach axis: 0 (air-off) is always prepended so every configuration includes a wind-off point.
"""
import re

def expand(cell, named=None, csv_loader=None, ensure_zero=False):
    named = named or {}
    cell = "" if cell is None else str(cell).strip()
    if cell == "" or cell.lower() == "none":
        return []
    out = []
    for tok in cell.split(","):
        t = tok.strip()
        if not t:
            continue
        if t.startswith("@"):
            out += list(named.get(t[1:], [])); continue
        if t.startswith("csv:"):
            out += (csv_loader(t[4:]) if csv_loader else [t]); continue
        ret = t.endswith(("R", "r"))
        core = t[:-1] if ret else t
        m = re.match(r'^(-?\d+\.?\d*):(-?\d+\.?\d*):(-?\d+\.?\d*)$', core)  # start:delta:end
        if m:
            a, d, b = float(m.group(1)), float(m.group(2)), float(m.group(3))
            step = abs(d) if b >= a else -abs(d)
            seq, v = [], a
            while (v <= b + 1e-9) if b >= a else (v >= b - 1e-9):
                seq.append(round(v, 6)); v = round(v + step, 6)
            leg = [int(x) if float(x).is_integer() else x for x in seq]
            out += leg + (leg[::-1][1:] if ret else [])
        else:
            v = float(core)
            out.append(int(v) if v.is_integer() else v)
    if ensure_zero and out and 0 not in out:
        out = [0] + out
    return out


def build_points(alpha, beta, mach, named=None):
    """Ordered (mach, beta, alpha) points. Mach outer (air-off first), alpha inner.
    Mach always includes 0 (air-off)."""
    A = expand(alpha, named) or [None]
    B = expand(beta, named) or [None]
    M = expand(mach, named, ensure_zero=True) or [0]
    return [(m, b, a) for m in M for b in B for a in A]


if __name__ == "__main__":
    tests = {
        "2": [2], "0,2,4": [0, 2, 4], "-4:2:8": [-4, -2, 0, 2, 4, 6, 8],
        "0:2:10R": [0, 2, 4, 6, 8, 10, 8, 6, 4, 2, 0],
        "-4:2:8, 10, 12": [-4, -2, 0, 2, 4, 6, 8, 10, 12], "0:2:9": [0, 2, 4, 6, 8], "": [],
    }
    named = {"alpha_fine": list(range(-4, 17))}
    ok = True
    for k, exp in tests.items():
        got = expand(k, named); good = got == exp; ok = ok and good
        print(f"  {'OK ' if good else 'FAIL'} {k!r:18} -> {got}")
    # mach air-off
    for k, exp in {"0.3": [0, 0.3], "0.3,0.5,0.7": [0, 0.3, 0.5, 0.7], "0": [0], "": []}.items():
        got = expand(k, ensure_zero=True); good = got == exp; ok = ok and good
        print(f"  {'OK ' if good else 'FAIL'} mach {k!r:12} -> {got}")
    print("\nnested (mach outer, air-off first):")
    for m, b, a in build_points("-4:4:4", "0", "0.3,0.5"):
        print(f"  M={m}  beta={b}  alpha={a}")
    print("\nALL PASS:", ok)
