r"""노이즈 제거 전후 비교 그림 (csi_io.preprocess / periodicity 기준).

사용
    cd E:\esp\wifi-sensing\pc
    python plot_denoise.py                      # ..\docsigures\denoise_compare.png
    (data\ 폴더에 20261121_155026, 20261121_163812, 20261121_170403 세션이 있어야 함)

(a) 호흡 구간 60초 예시 창(제거 후 점수가 호흡 창들의 중앙값에 가장 가까운 창): PC1 호흡 대역 파형, 원본 vs 제거 후
(b) PC1 평균 스펙트럼: 빈 방 vs 호흡, 원본(점선) vs 제거 후(실선)
(c) 창별 주기성 점수 분포: 원본 / 제거 후 × 빈 방 / 호흡, 빈 방 90% 선
(d) 숨 참기 세션(COM5) 시간축: 12초 창 점수, hold 구간 음영
"""
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from pathlib import Path

import csi_io  # noqa: E402

FS = 10.0
REPO = Path(__file__).resolve().parent.parent
DATA = str(REPO / "data")
OUT = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "docs" / "figures" / "denoise_compare.png")

# 팔레트 (dataviz reference, light): slot1 blue = 빈 방, slot2 orange = 호흡
C_EMPTY, C_BREATH = "#2a78d6", "#eb6834"
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
RAW_GRAY = "#9a9893"

plt.rcParams.update({
    "font.family": "Malgun Gothic", "axes.unicode_minus": False, "font.size": 9,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.titlecolor": INK, "axes.titlesize": 10, "axes.titleweight": "bold",
    "figure.facecolor": SURF, "axes.facecolor": SURF, "legend.frameon": False,
    "axes.spines.top": False, "axes.spines.right": False,
})


def pc1(W):
    B = np.stack([csi_io._bandpass(W[:, j], FS, *csi_io.ANALYSIS_BAND) for j in range(W.shape[1])], axis=1)
    B = (B - B.mean(0)) / (B.std(0) + 1e-9)
    u, _, _ = np.linalg.svd(B, full_matrices=False)
    return u[:, 0]


def windows(grid, A, df, label, win, step, edge):
    sc = csi_io.breathing_scores(grid, A, df, win=win, step=step, edge=edge)
    sc = sc[sc.label == label].reset_index(drop=True)
    idx = np.searchsorted(grid, sc.t0.to_numpy())
    return sc, idx


def spectrum(A, idx, n):
    f = np.fft.rfftfreq(n, 1 / FS)
    m = (f >= 0.05) & (f <= 2.0)
    acc = []
    for i in idx:
        x = pc1(A[i:i + n])
        P = np.abs(np.fft.rfft(x * np.hanning(n))) ** 2
        acc.append(P / P[m].sum())
    return 60 * f, np.mean(acc, axis=0)


sessions = {
    "empty": (rf"{DATA}\20261121_155026\COM4.csv", "empty"),
    "breath": (rf"{DATA}\20261121_163812\COM4.csv", "breath"),
}
res = {}
for key, (path, lab) in sessions.items():
    df, amp = csi_io.load_csi(path, verbose=False)
    for stage, kw in (("raw", dict(gain_norm=False, outlier=False)), ("clean", {})):
        grid, A = csi_io.preprocess(df, amp, **kw)
        sc, idx = windows(grid, A, df, lab, 60, 30, 30)
        res[(key, stage)] = (grid, A, sc, idx)

fig, axes = plt.subplots(2, 2, figsize=(13, 8.2))
fig.suptitle("CSI 노이즈 제거 전후 비교 — 이득 정규화 + Hampel 이상치 제거 ((a)~(c) COM4, (d) COM5 · 2026-11-21)",
             fontsize=12, fontweight="bold", color=INK, x=0.01, ha="left")

# (a) 예시 파형
ax = axes[0, 0]
n = int(60 * FS)
_, _, sc_c, idx_c = res[("breath", "clean")]
k = int(np.argmin(np.abs(sc_c.score - sc_c.score.median())))
i0 = idx_c[k]
t = np.arange(n) / FS
for stage, color, off, name in (("raw", RAW_GRAY, 3.0, "원본"), ("clean", C_BREATH, 0.0, "노이즈 제거 후")):
    grid, A, sc, _ = res[("breath", stage)]
    x = csi_io._bandpass(pc1(A[i0:i0 + n]), FS, *csi_io.BREATH_BAND)
    x = x / (x.std() + 1e-9)
    s, b = csi_io.periodicity(A[i0:i0 + n], FS)
    ax.plot(t, x + off, color=color, lw=1.6)
    ax.text(60.8, off, f"{name}\n점수 {s:.2f}\n피크 {b:.0f}회/분", color=INK2, fontsize=8, va="center")
ax.set_xlim(0, 60)
ax.set_yticks([])
ax.set_xlabel("시간 (초)")
ax.set_title("(a) 누워서 호흡 — 60초 예시 창, PC1 호흡 대역(0.1~0.5Hz) 파형")
ax.text(0, -2.6, "예시 창: 노이즈 제거 후 점수가 호흡 창 17개의 중앙값에 가장 가까운 창 (가장 잘 나온 창이 아님)",
        fontsize=7.5, color=INK2)
ax.grid(axis="x", color=GRID, lw=0.6)

# (b) 평균 스펙트럼
ax = axes[0, 1]
ax.axvspan(6, 30, color=GRID, alpha=0.5, lw=0)
ax.text(18, 0.0, "호흡 대역 6~30회/분", ha="center", va="bottom", fontsize=8, color=INK2)
for key, color, name in (("empty", C_EMPTY, "빈 방"), ("breath", C_BREATH, "누워서 호흡")):
    for stage, ls, lw in (("raw", (0, (3, 2)), 1.2), ("clean", "-", 2.0)):
        grid, A, sc, idx = res[(key, stage)]
        bpm, P = spectrum(A, idx, n)
        sel = bpm <= 60
        ax.plot(bpm[sel], P[sel], color=color, ls=ls, lw=lw,
                label=f"{name} · {'원본' if stage == 'raw' else '제거 후'}")
ax.set_xlim(0, 60)
ax.set_ylim(bottom=0)
ax.set_xlabel("주파수 (회/분)")
ax.set_ylabel("정규화 파워 (창 평균)")
ax.set_title("(b) PC1 평균 스펙트럼 — 빈 방 vs 호흡")
ax.legend(fontsize=8, loc="upper right")
ax.grid(axis="y", color=GRID, lw=0.6)

# (c) 점수 분포
ax = axes[1, 0]
rng = np.random.default_rng(0)
pos = {("empty", "raw"): 0, ("breath", "raw"): 1, ("empty", "clean"): 3, ("breath", "clean"): 4}
for (key, stage), x in pos.items():
    s = res[(key, stage)][2].score.to_numpy()
    color = C_EMPTY if key == "empty" else C_BREATH
    ax.scatter(x + rng.uniform(-0.18, 0.18, len(s)), s, s=16, color=color, alpha=0.75,
               edgecolors=SURF, linewidths=0.6, zorder=3)
    ax.plot([x - 0.3, x + 0.3], [np.median(s)] * 2, color=INK, lw=2, zorder=4)
for stage, x0 in (("raw", 0), ("clean", 3)):
    e = res[("empty", stage)][2].score.to_numpy()
    b = res[("breath", stage)][2].score.to_numpy()
    p90 = np.percentile(e, 90)
    ax.plot([x0 - 0.45, x0 + 1.45], [p90] * 2, color=INK2, lw=1, ls=(0, (2, 2)), zorder=2)
    ax.text(x0 - 0.45, p90, "빈 방 90%선", fontsize=7.5, color=INK2, va="bottom", ha="left")
    ax.text(x0 + 0.5, 0.93, f"호흡 창 중 90%선 초과 {100*np.mean(b > p90):.0f}%", fontsize=8.5,
            color=INK, ha="center", va="top", transform=ax.get_xaxis_transform(), fontweight="bold")
ax.set_xticks([0, 1, 3, 4], ["빈 방", "호흡", "빈 방", "호흡"])
ax.text(0.5, 1.0, "원본", transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=9, color=INK2)
ax.text(3.5, 1.0, "노이즈 제거 후", transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=9, color=INK2)
ax.set_xlim(-0.7, 4.7)
ax.set_ylim(top=ax.get_ylim()[1] * 1.12)
ax.set_ylabel("주기성 점수 (60초 창)")
ax.set_title("(c) 창별 주기성 점수 — 검은 막대 = 중앙값 (우연이면 초과 10%)", pad=16)
ax.grid(axis="y", color=GRID, lw=0.6)

# (d) 숨 참기 타임라인 (COM5)
ax = axes[1, 1]
path = rf"{DATA}\20261121_170403\COM5.csv"
df, amp = csi_io.load_csi(path, verbose=False)
t0 = df.pc_time.iloc[0]
lab = df.label.to_numpy()
tt = df.pc_time.to_numpy() - t0
chg = np.r_[0, np.where(lab[1:] != lab[:-1])[0] + 1, len(lab)]
for a, b in zip(chg[:-1], chg[1:]):
    if lab[a] == "hold":
        ax.axvspan(tt[a], tt[b - 1], color=GRID, alpha=0.8, lw=0)
for stage, color, lw, name in (("raw", RAW_GRAY, 1.0, "원본"), ("clean", C_BREATH, 1.8, "노이즈 제거 후")):
    kw = dict(gain_norm=False, outlier=False) if stage == "raw" else {}
    grid, A = csi_io.preprocess(df, amp, **kw)
    sc = csi_io.breathing_scores(grid, A, None, win=12, step=1)
    ax.plot(sc.t0 - t0 + 6, sc.score, color=color, lw=lw, label=name)
ax.text(0.99, 0.98, "회색 음영 = 숨 참기(hold)", transform=ax.transAxes, ha="right", va="top", fontsize=8, color=INK2)
ax.set_xlabel("시간 (초)")
ax.set_ylabel("주기성 점수 (12초 창, 창 중심)")
ax.set_title("(d) 숨 참기 세션 (COM5) — 숨 참는 동안 점수가 내려가야 정상")
ax.legend(fontsize=8, loc="upper left")
ax.grid(axis="y", color=GRID, lw=0.6)

fig.text(0.01, 0.005,
         "데이터: 빈 방 20261121_155026(26분), 누워서 호흡 20261121_163812(10분), 숨 참기 20261121_170403(5.5분). "
         "COM4 RSSI 약 -82dBm로 신호가 약한 상태. 점수 = PC1의 0.1~0.5Hz 최대 피크 파워 / 0.05~2Hz 전체 파워.",
         fontsize=7.5, color=INK2)
fig.tight_layout(rect=(0, 0.02, 1, 0.96))
fig.savefig(OUT, dpi=130)
print("saved", OUT)
