r"""
전처리(preprocess.py) 전후 비교 그림.

사용
    cd E:\esp\wifi-sensing\pc
    python plot_preprocess.py                                   # 숨 참기 세션 COM5 → ..\docs\figures\preprocess_compare.png
    python plot_preprocess.py --session 20261121_163812_breath --rx COM4 --start 120

전제: ..\processed\<세션>\<RX>.npz 가 있어야 함 (python preprocess.py 먼저). 원본은 data\ 또는 dataset\ 에서 찾는다.

패널
  (a) 원본 진폭 히트맵: 192 서브캐리어 전부, 패킷 단위 (가드·DC 0 포함, 이득 변동 그대로)
  (b) 전처리 후 히트맵: 유효 서브캐리어만, 10Hz 격자, 이득 정규화 + Hampel
  (c) 서브캐리어 하나: 원본(패킷 점) vs 전처리 후(선), 각자 평균 대비 변화율(%)
  (d) 패킷별 전체 진폭(L2 크기): 원본은 AGC로 오르내림 → 정규화 후에는 1로 고정 (서브캐리어 간 비율만 남음)
  (e) 시간축: PC 수신 시각 간격(시리얼 버퍼링으로 흔들림) vs id 기준 격자(정확히 0.1초)
  (f) 라벨 정리: 원본 라벨 → 통일된 클래스 + 학습에 쓰는 구간(valid)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import csi_io
from preprocess import CLASS_OF, find_rx_files, load_processed, normalize_label

REPO = Path(__file__).resolve().parent.parent
RAW, CLEAN = "#9a9893", "#eb6834"
CLS_COLOR = {"empty": "#2a78d6", "static": "#eb6834", "hold": "#1baf7a", "motion": "#6250d6"}
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({
    "font.family": "Malgun Gothic", "axes.unicode_minus": False, "font.size": 9,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.titlecolor": INK, "axes.titlesize": 10, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "figure.facecolor": SURF, "axes.facecolor": SURF, "legend.frameon": False,
    "axes.spines.top": False, "axes.spines.right": False,
})


def find_session(name: str) -> Path:
    for base in (REPO / "dataset", REPO / "data"):
        p = base / name
        if p.is_dir():
            return p
        hits = sorted(base.glob(name + "*"))
        if hits:
            return hits[0]
    raise SystemExit(f"세션 폴더를 찾을 수 없습니다: {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="20261121_170403_hold")
    ap.add_argument("--rx", default="COM5")
    ap.add_argument("--start", type=float, default=30.0, help="(a)~(d) 확대 구간 시작(세션 시작 기준 초)")
    ap.add_argument("--span", type=float, default=60.0, help="확대 구간 길이(초)")
    ap.add_argument("--out", default=str(REPO / "docs" / "figures" / "preprocess_compare.png"))
    a = ap.parse_args()

    sess_dir = find_session(a.session)
    raw_path = find_rx_files(sess_dir)[a.rx]
    df, amp = csi_io.load_csi(raw_path, verbose=False)
    proc = load_processed(REPO / "processed" / sess_dir.name / f"{a.rx}.npz")

    t_raw = df["pc_time"].to_numpy(float)
    T0 = t_raw[0]
    tr = t_raw - T0
    tp = proc["t"] - T0
    w0, w1 = a.start, a.start + a.span
    mr = (tr >= w0) & (tr < w1)
    mp = (tp >= w0) & (tp < w1)

    fig = plt.figure(figsize=(13.5, 11))
    gs = fig.add_gridspec(3, 2, height_ratios=[1.15, 1, 1], hspace=0.5, wspace=0.32)
    fig.suptitle(f"전처리 전후 비교 — {sess_dir.name} / {a.rx} (preprocess.py)",
                 fontsize=12, fontweight="bold", color=INK, x=0.01, ha="left")

    # (a) 원본 히트맵
    ax = fig.add_subplot(gs[0, 0])
    A = np.nan_to_num(amp[mr])
    vmax = np.percentile(A, 99)
    ax.imshow(A.T, aspect="auto", origin="lower", extent=[tr[mr][0], tr[mr][-1], 0, A.shape[1]],
              cmap="viridis", vmin=0, vmax=vmax)
    for name, sl in csi_io.subcarrier_segments(A.shape[1]).items():
        if sl.start:
            ax.axhline(sl.start, color="w", lw=0.8, ls="--")
        ax.text(tr[mr][0] + 0.5, sl.start + 2, name, color="w", fontsize=8, va="bottom")
    null = csi_io.null_subcarriers(amp)
    ax.set_title(f"(a) 원본: 서브캐리어 {A.shape[1]}개, 패킷 {mr.sum()}개 ({mr.sum() / a.span:.1f}Hz, 불규칙)")
    ax.set_ylabel("서브캐리어 번호")
    ax.set_xlabel("시간 (초)")
    ax.text(0.99, -0.27, f"어두운 가로줄 = 항상 0인 가드·DC 서브캐리어 {int(null.sum())}개",
            transform=ax.transAxes, ha="right", fontsize=7.5, color=INK2)

    # (b) 전처리 후 히트맵
    ax = fig.add_subplot(gs[0, 1])
    P = proc["amp"][mp]
    ax.imshow(P.T, aspect="auto", origin="lower", extent=[tp[mp][0], tp[mp][-1], 0, P.shape[1]],
              cmap="viridis", vmin=np.percentile(P, 1), vmax=np.percentile(P, 99))
    sc = proc["sc_index"]
    split = int(np.searchsorted(sc, 64))
    ax.axhline(split, color="w", lw=0.8, ls="--")
    ax.text(tp[mp][0] + 0.5, 2, "lltf", color="w", fontsize=8, va="bottom")
    ax.text(tp[mp][0] + 0.5, split + 2, "htltf", color="w", fontsize=8, va="bottom")
    ax.set_title(f"(b) 전처리 후: 유효 서브캐리어 {P.shape[1]}개, 격자 {mp.sum()}점 (정확히 10Hz)")
    ax.set_ylabel("유효 서브캐리어 (0인 것 제외)")
    ax.set_xlabel("시간 (초)")
    ax.text(0.99, -0.27, "이득 정규화 + Hampel 이상치 제거 → 서브캐리어 간 상대 모양만 남음",
            transform=ax.transAxes, ha="right", fontsize=7.5, color=INK2)

    # (c) 서브캐리어 하나
    ax = fig.add_subplot(gs[1, 0])
    j = int(np.argmax(np.nanstd(proc["amp"], axis=0)))    # 전처리 후 변화가 가장 큰 서브캐리어
    orig = int(sc[j])
    x_raw = amp[mr, orig]
    x_pro = proc["amp"][mp, j]
    ax.scatter(tr[mr], 100 * (x_raw / np.nanmean(x_raw) - 1), s=4, color=RAW, label="원본 (패킷마다)", zorder=2)
    ax.plot(tp[mp], 100 * (x_pro / np.mean(x_pro) - 1), color=CLEAN, lw=1.6, label="전처리 후 (10Hz)", zorder=3)
    ax.set_title(f"(c) 서브캐리어 {orig}번: 원본의 튀는 값·이득 계단이 정리됨")
    ax.set_ylabel("평균 대비 변화 (%)")
    ax.set_xlabel("시간 (초)")
    ax.legend(fontsize=8, loc="upper right", ncol=2)
    ax.grid(axis="y", color=GRID, lw=0.6)

    # (d) 패킷별 전체 진폭
    ax = fig.add_subplot(gs[1, 1])
    keep = ~null
    keep[:2] = False
    norm_raw = np.sqrt(np.nansum(np.nan_to_num(amp[:, keep]) ** 2, axis=1))
    ax.plot(tr[mr], 100 * (norm_raw[mr] / norm_raw[mr].mean() - 1), color=RAW, lw=1.0, label="원본: AGC 이득 변동")
    ax.axhline(0, color=CLEAN, lw=1.8, label="정규화 후: 0% (항상 1)")
    cv = 100 * norm_raw.std() / norm_raw.mean()
    ax.set_title(f"(d) 패킷 전체 진폭: 세션 전체 변동계수 {cv:.1f}% → 0%")
    ax.set_ylabel("평균 대비 변화 (%)")
    ax.set_xlabel("시간 (초)")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(axis="y", color=GRID, lw=0.6)

    # (e) 시간축
    ax = fig.add_subplot(gs[2, 0])
    d_pc = np.diff(t_raw) * 1000
    d_pc = d_pc[(d_pc > 0) & (d_pc < 120)]
    ax.hist(d_pc, bins=np.arange(0, 121, 2), color=RAW, label=f"PC 수신 시각 간격 (원본, median {np.median(d_pc):.1f}ms)")
    ax.axvline(20, color=INK, lw=1, ls=(0, (2, 2)))
    ax.text(21, ax.get_ylim()[1] * 0.92, "TX 주기 20ms", fontsize=8, color=INK)
    q = proc["meta"]["quality"]
    ax.set_title(f"(e) 시간축: pc_time 대신 id × 20ms (손실 {q['loss_pct']}%의 위치도 정확)")
    ax.set_xlabel("연속 수신 패킷 사이 간격 (ms)")
    ax.set_ylabel("횟수")
    ax.text(0.97, 0.75, "전처리 후: id 기준 격자\n간격이 항상 정확히 100ms(10Hz)\n0.2초 넘는 수신 공백은 valid=False",
            transform=ax.transAxes, ha="right", va="top", fontsize=8.5, color=INK,
            bbox=dict(boxstyle="round,pad=0.4", fc=SURF, ec=CLEAN, lw=1.2))
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(axis="y", color=GRID, lw=0.6)

    # (f) 라벨 정리
    ax = fig.add_subplot(gs[2, 1])
    raw_lab = df["label"].astype(str).to_numpy()
    def bands(t, lab, y, h, color_of):
        chg = np.r_[0, np.where(lab[1:] != lab[:-1])[0] + 1, len(lab)]
        for s, e in zip(chg[:-1], chg[1:]):
            ax.add_patch(plt.Rectangle((t[s], y), t[e - 1] - t[s], h, color=color_of(lab[s]), lw=0))
            if t[e - 1] - t[s] > 12:
                ax.text((t[s] + t[e - 1]) / 2, y + h / 2, lab[s], ha="center", va="center", fontsize=7, color="w")
    cls_of_raw = lambda l: CLS_COLOR.get(CLASS_OF.get(normalize_label(l), ""), "#c9c7c0")
    bands(tr, raw_lab, 2.2, 0.8, cls_of_raw)
    bands(tp, proc["cls"], 1.1, 0.8, lambda c: CLS_COLOR.get(c, "#c9c7c0"))
    v = proc["valid"]
    bands(tp, np.where(v, "valid", "제외"), 0.0, 0.8, lambda s: INK2 if s == "valid" else "#e4e3df")
    ax.set_xlim(tp[0], tp[-1])
    ax.set_ylim(-0.1, 3.1)
    ax.set_yticks([0.4, 1.5, 2.6], ["학습에 쓰는 구간\n(valid)", "전처리 후 클래스", "원본 라벨"])
    ax.set_xlabel("시간 (초, 세션 전체)")
    ax.set_title(f"(f) 라벨 정리: 이름 통일 → 클래스, 경계 {proc['meta']['edge']:g}초·공백 제외 (유효 {100 * v.mean():.0f}%)")
    for s in ("left", "bottom"):
        ax.spines[s].set_visible(False)

    fig.text(0.01, 0.005, f"확대 구간 {w0:g}~{w1:g}초. 원본 = 로거 저장 파일(손상 행은 이미 로거가 거름), "
             f"전처리 후 = processed\\{sess_dir.name}\\{a.rx}.npz. 품질: 저장 {q['rate_hz']}Hz, 손실 {q['loss_pct']}%, RSSI {q['rssi_mean']} dBm.",
             fontsize=7.5, color=INK2)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=125, bbox_inches="tight")
    print("저장:", out)


if __name__ == "__main__":
    main()
