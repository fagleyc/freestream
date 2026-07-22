"""Regenerate the FB_Cal_GUI loading-guide icons.

Sign convention (2026-07 rework): body axes x forward, y right, z down.
Positive pitch = nose up, positive yaw = nose right, positive roll =
right wing down, positive axial = aft (drag direction). The dead-weight
load always acts straight down; pos/neg orientations differ by model
orientation on the rig (upright / inverted / rolled 90 deg), never by
an impossible "upward" weight.

Side views are drawn nose-LEFT (so body +x points screen-left); roll
icons are a rear view looking forward (+y = screen right, +roll =
clockwise). Moments are applied by hanging the weight at the OPPOSITE
station (arm = dx1 + dx2).

Run:  python tools/gen_cal_icons.py     (from balance_cal/)
Originals are preserved in FB_Cal_GUI/_originals/ on first run.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import (Circle, FancyArrow, FancyArrowPatch,
                                Polygon, Rectangle)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "FB_Cal_GUI"
BACKUP = OUT / "_originals"

BLUE = "#4472C4"
DBLUE = "#2E74B5"
GREEN = "#70AD47"
ORANGE = "#FFC000"
GRAY = "#7F7F7F"
RED = "#C00000"

W, H = 8.86, 3.0          # inches at 100 dpi -> 886 x 300 px

# geometry (side view)
BAR_X0, BAR_X1 = 1.55, 7.30
BAR_Y0, BAR_Y1 = 1.15, 1.95
CX = (BAR_X0 + BAR_X1) / 2          # balance center
FWD_X, AFT_X = 2.65, 6.30           # fwd station (near nose), aft station
BAR_MID = (BAR_Y0 + BAR_Y1) / 2


def _canvas(title: str):
    fig = plt.figure(figsize=(W, H), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")
    ax.add_patch(Rectangle((0.04, 0.04), W - 0.08, H - 0.08, fill=False,
                           edgecolor=DBLUE, lw=1.5))
    ax.text(0.18, H - 0.40, title, fontsize=16, color="black")
    return fig, ax


def _convention_note(ax):
    ax.text(W - 0.18, 0.14, "x fwd, y right, z down",
            fontsize=8, color=GRAY, ha="right")


def _gauge(ax, x, active: bool, label: str):
    if active:                       # loaded gauge drawn doubled
        ax.add_patch(Rectangle((x - 0.17, BAR_MID - 0.15), 0.16, 0.30,
                               facecolor=GREEN, edgecolor="#507E32"))
        ax.add_patch(Rectangle((x + 0.01, BAR_MID - 0.15), 0.16, 0.30,
                               facecolor=GREEN, edgecolor="#507E32"))
    else:
        ax.add_patch(Rectangle((x - 0.15, BAR_MID - 0.15), 0.30, 0.30,
                               facecolor=GREEN, edgecolor="#507E32"))
    ax.text(x, BAR_Y1 + 0.16, label, fontsize=13, ha="center")


def _dim(ax, x0, x1, y, label):
    ax.add_patch(FancyArrowPatch((x0, y), (x1, y),
                                 arrowstyle="<->", mutation_scale=12,
                                 color=DBLUE, lw=1.2))
    ax.text((x0 + x1) / 2, y - 0.22, label, fontsize=12, ha="center",
            color="black")


def _down_arrow(ax, x, y0, y1, label, color=DBLUE, lx=0.12):
    ax.add_patch(FancyArrow(x, y0, 0, y1 - y0, width=0.012,
                            head_width=0.09, head_length=0.10,
                            length_includes_head=True, color=color))
    ax.text(x + lx, y1 + 0.05, label, fontsize=12, color="black")


def _side_body(ax, gauges, dim_labels):
    """Bar with nose LEFT, sting stub RIGHT. gauges = (fwd_lbl, aft_lbl,
    active: 'fwd'|'aft'|None)."""
    # sting (aft support)
    ax.add_patch(Rectangle((BAR_X1, BAR_MID - 0.13), 0.95, 0.26,
                           facecolor=GRAY, edgecolor="#555555",
                           hatch="///"))
    ax.text(BAR_X1 + 0.47, BAR_MID - 0.38, "sting (aft)", fontsize=9,
            color=GRAY, ha="center")
    # body + nose cone
    ax.add_patch(Rectangle((BAR_X0, BAR_Y0), BAR_X1 - BAR_X0,
                           BAR_Y1 - BAR_Y0, facecolor=BLUE,
                           edgecolor=DBLUE))
    ax.add_patch(Polygon([(BAR_X0, BAR_Y0), (BAR_X0, BAR_Y1),
                          (0.85, BAR_MID)], facecolor=BLUE,
                         edgecolor=DBLUE))
    ax.text(0.85, BAR_Y0 - 0.25, "nose (fwd)", fontsize=9, color=GRAY,
            ha="left")
    fwd_lbl, aft_lbl, active = gauges
    _gauge(ax, FWD_X, active == "fwd", fwd_lbl)
    _gauge(ax, AFT_X, active == "aft", aft_lbl)
    ax.add_patch(Circle((CX, BAR_MID), 0.16, facecolor=ORANGE,
                        edgecolor="#BF9000"))
    d1, d2 = dim_labels
    _dim(ax, FWD_X, CX, 0.88, d1)
    _dim(ax, CX, AFT_X, 0.88, d2)


def _axes_glyph(ax, y_sym: str):
    """x-forward arrow (screen-left), Ax+ aft, and the y in/out symbol."""
    ax.add_patch(FancyArrow(8.35, H - 0.55, -0.62, 0, width=0.012,
                            head_width=0.09, head_length=0.10,
                            length_includes_head=True, color="black"))
    ax.text(7.95, H - 0.42, "x (fwd)", fontsize=11, ha="center")
    ax.add_patch(FancyArrow(7.85, H - 0.88, 0.55, 0, width=0.012,
                            head_width=0.09, head_length=0.10,
                            length_includes_head=True, color=RED))
    ax.text(8.10, H - 1.14, "Ax$^+$ (aft)", fontsize=10, ha="center",
            color=RED)
    if y_sym:
        ax.text(W - 0.18, 0.38, y_sym, fontsize=9, ha="right",
                color=GRAY)


def _orientation(ax, mode: str):
    """z arrow at the balance center + caption. mode: upright|inverted"""
    if mode == "upright":
        _down_arrow(ax, CX, BAR_Y0, 0.50, "z", color="black")
        ax.text(0.18, 0.14, "model upright", fontsize=11, color=GRAY)
    else:
        ax.add_patch(FancyArrow(CX, BAR_Y1, 0, 0.42, width=0.012,
                                head_width=0.09, head_length=0.10,
                                length_includes_head=True, color="black"))
        ax.text(CX + 0.14, BAR_Y1 + 0.28, "z", fontsize=12)
        ax.text(0.18, 0.14, "MODEL INVERTED (rolled 180°)",
                fontsize=11, color=RED)


def _rolled(ax, wing_down: str, axis_label: str):
    """90-deg rolled model for yaw / side-force loads. The loaded body
    axis (+y) points down when the right wing is down."""
    if wing_down == "right":
        _down_arrow(ax, CX, BAR_Y0, 0.50, axis_label, color="black")
        ax.text(0.18, 0.14,
                "model rolled 90° — RIGHT wing down (z out of page)",
                fontsize=11, color=GRAY)
    else:
        ax.add_patch(FancyArrow(CX, BAR_Y1, 0, 0.42, width=0.012,
                                head_width=0.09, head_length=0.10,
                                length_includes_head=True, color="black"))
        ax.text(CX + 0.14, BAR_Y1 + 0.28, axis_label, fontsize=12)
        ax.text(0.18, 0.14,
                "model rolled 90° — LEFT wing down (z into page)",
                fontsize=11, color=RED)


def side_icon(fname, title, gauges, dims, load_at, orient,
              rolled_axis=None, load_label="Load (weight)"):
    fig, ax = _canvas(title)
    _side_body(ax, gauges, dims)
    x = FWD_X if load_at == "fwd" else AFT_X
    _down_arrow(ax, x, BAR_Y0, 0.35, load_label)
    if rolled_axis:
        _rolled(ax, orient, rolled_axis)
        _axes_glyph(ax, "")
    else:
        _orientation(ax, orient)
        y_sym = ("y ⊗ (into page)" if orient == "upright"
                 else "y ⊙ (out of page)")
        _axes_glyph(ax, y_sym)
    _convention_note(ax)
    fig.savefig(OUT / fname)
    plt.close(fig)


def axial_icon(fname, title, direction):
    """Horizontal cable pull along the body axis, model upright.
    direction 'aft' (pos, screen-right) or 'fwd' (neg, screen-left)."""
    fig, ax = _canvas(title)
    _side_body(ax, ("N1", "N2", None), ("dx1", "dx2"))
    y = BAR_Y1 + 0.55
    if direction == "aft":
        ax.add_patch(FancyArrow(CX - 0.75, y, 1.5, 0, width=0.014,
                                head_width=0.10, head_length=0.12,
                                length_includes_head=True, color=DBLUE))
        ax.text(CX, y + 0.14, "Load — pull AFT via cable/pulley",
                fontsize=11, ha="center")
    else:
        ax.add_patch(FancyArrow(CX + 0.75, y, -1.5, 0, width=0.014,
                                head_width=0.10, head_length=0.12,
                                length_includes_head=True, color=DBLUE))
        ax.text(CX, y + 0.14, "Load — pull FWD via cable/pulley",
                fontsize=11, ha="center")
    _down_arrow(ax, CX, BAR_Y0, 0.50, "z", color="black")
    ax.text(0.18, 0.14, "model upright", fontsize=11, color=GRAY)
    _axes_glyph(ax, "y ⊗ (into page)")
    _convention_note(ax)
    fig.savefig(OUT / fname)
    plt.close(fig)


def roll_icon(fname, title, side):
    """Rear view (looking forward): +y = screen right, +roll = CW =
    right wing down. Weight hangs at the roll-bar end on `side`."""
    fig, ax = _canvas(title)
    cx, cy = W / 2, 1.30
    # roll bar through the model cross-section
    ax.add_patch(Rectangle((cx - 2.3, cy - 0.07), 4.6, 0.14,
                           facecolor=GRAY, edgecolor="#555555"))
    ax.add_patch(Circle((cx, cy), 0.52, facecolor=BLUE, edgecolor=DBLUE))
    ax.add_patch(Circle((cx, cy), 0.14, facecolor=ORANGE,
                        edgecolor="#BF9000"))
    ax.text(cx - 1.6, cy - 0.75, "x ⊗ (fwd, into page)", fontsize=10,
            ha="center", color=GRAY)
    hang = cx + 2.2 if side == "right" else cx - 2.2
    _down_arrow(ax, hang, cy - 0.08, 0.38, "Load (weight)",
                lx=0.12 if side == "right" else -1.45)
    _dim(ax, min(cx, hang), max(cx, hang), cy - 0.45, "dr")
    # +roll sense indicator (clockwise when looking forward)
    arc = FancyArrowPatch((cx - 0.80, cy + 0.75), (cx + 0.80, cy + 0.75),
                          connectionstyle="arc3,rad=-0.40",
                          arrowstyle="-|>", mutation_scale=16,
                          color=RED, lw=1.6)
    ax.add_patch(arc)
    ax.text(cx, cy + 1.12, "+Roll = right wing down", fontsize=10,
            ha="center", color=RED)
    # axes: y right, z down
    ax.add_patch(FancyArrow(7.6, H - 0.55, 0.62, 0, width=0.012,
                            head_width=0.09, head_length=0.10,
                            length_includes_head=True, color="black"))
    ax.text(7.9, H - 0.42, "y (right)", fontsize=11, ha="center")
    ax.add_patch(FancyArrow(8.45, H - 0.75, 0, -0.55, width=0.012,
                            head_width=0.09, head_length=0.10,
                            length_includes_head=True, color="black"))
    ax.text(8.45, H - 1.55, "z", fontsize=11, ha="center")
    ax.text(0.18, 0.14, "REAR VIEW (looking forward), model upright",
            fontsize=11, color=GRAY)
    _convention_note(ax)
    fig.savefig(OUT / fname)
    plt.close(fig)


def main() -> None:
    BACKUP.mkdir(exist_ok=True)
    for p in sorted(OUT.glob("*.png")):
        dst = BACKUP / p.name
        if not dst.exists():
            shutil.copy2(p, dst)

    # ── Moment balance: pitch (moment at gauge, weight at opposite
    #    station; +pitch = nose up) ────────────────────────────────────
    side_icon("MB_Aft_Pitch_pos.png",
              "AP – Pos (Side View)  [+pitch: nose up]",
              ("FP", "AP", "aft"), ("dx1", "dx2"),
              load_at="fwd", orient="inverted")
    side_icon("MB_Aft_Pitch_neg.png", "AP – Neg (Side View)",
              ("FP", "AP", "aft"), ("dx1", "dx2"),
              load_at="fwd", orient="upright")
    side_icon("MB_Fwd_Pitch_pos.png",
              "FP – Pos (Side View)  [+pitch: nose up]",
              ("FP", "AP", "fwd"), ("dx1", "dx2"),
              load_at="aft", orient="upright")
    side_icon("MB_Fwd_Pitch_neg.png", "FP – Neg (Side View)",
              ("FP", "AP", "fwd"), ("dx1", "dx2"),
              load_at="aft", orient="inverted")

    # ── Moment balance: yaw (+yaw = nose right; roll model 90°) ───────
    side_icon("MB_Aft_Yaw_pos.png",
              "AY – Pos (Side View)  [+yaw: nose right]",
              ("FY", "AY", "aft"), ("dy1", "dy2"),
              load_at="fwd", orient="right", rolled_axis="y")
    side_icon("MB_Aft_Yaw_neg.png", "AY – Neg (Side View)",
              ("FY", "AY", "aft"), ("dy1", "dy2"),
              load_at="fwd", orient="left", rolled_axis="y")
    side_icon("MB_Fwd_Yaw_pos.png",
              "FY – Pos (Side View)  [+yaw: nose right]",
              ("FY", "AY", "fwd"), ("dy1", "dy2"),
              load_at="aft", orient="left", rolled_axis="y")
    side_icon("MB_Fwd_Yaw_neg.png", "FY – Neg (Side View)",
              ("FY", "AY", "fwd"), ("dy1", "dy2"),
              load_at="aft", orient="right", rolled_axis="y")

    # ── Force balance: normal gauges (direct load at own station;
    #    +N = +z = down) ──────────────────────────────────────────────
    for name, station in (("N1", "fwd"), ("N2", "aft")):
        side_icon(f"FB_{name}_pos.png",
                  f"{name} – Pos (Side View)  [+N: z down]",
                  ("N1", "N2", station), ("dx1", "dx2"),
                  load_at=station, orient="upright")
        side_icon(f"FB_{name}_neg.png", f"{name} – Neg (Side View)",
                  ("N1", "N2", station), ("dx1", "dx2"),
                  load_at=station, orient="inverted")

    # ── Force balance: side gauges (+Y = +y = right; roll 90°) ────────
    for name, station in (("Y1", "fwd"), ("Y2", "aft")):
        side_icon(f"FB_{name}_pos.png",
                  f"{name} – Pos (Side View)  [+Y: right]",
                  ("Y1", "Y2", station), ("dy1", "dy2"),
                  load_at=station, orient="right", rolled_axis="y")
        side_icon(f"FB_{name}_neg.png", f"{name} – Neg (Side View)",
                  ("Y1", "Y2", station), ("dy1", "dy2"),
                  load_at=station, orient="left", rolled_axis="y")

    # ── Axial (+Ax = aft) and Roll (+roll = right wing down) ──────────
    for prefix in ("FB", "MB"):
        axial_icon(f"{prefix}_Axial_pos.png",
                   "Ax – Pos (Side View)", "aft")
        axial_icon(f"{prefix}_Axial_neg.png",
                   "Ax – Neg (Side View)", "fwd")
        roll_icon(f"{prefix}_Roll_pos.png",
                  "Roll – Pos (Rear View)  [+roll: right wing down]",
                  "right")
        roll_icon(f"{prefix}_Roll_neg.png",
                  "Roll – Neg (Rear View)", "left")

    print(f"Icons written to {OUT}")
    print(f"Originals preserved in {BACKUP}")


if __name__ == "__main__":
    main()
