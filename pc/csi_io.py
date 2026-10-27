"""
저장된 CSI CSV를 불러와 진폭 행렬로 바꾸고, 빠르게 눈으로 확인하는 도구.

사용 예
    python csi_io.py logs/20261027_210000/COM5.csv          # 히트맵 PNG 저장
    python csi_io.py logs/.../sim.csv --show                 # 창으로 보기

코드에서
    from csi_io import load_csi
    df, amp = load_csi("logs/.../COM5.csv")   # amp: (패킷 수, 서브캐리어 수)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def parse_values(s: str) -> np.ndarray:
    return np.fromstring(s.strip().strip('"').strip("[]"), sep=",", dtype=np.float32)


def load_csi(path: str | Path, drop_invalid_first_word: bool = True):
    """CSV → (메타데이터 DataFrame, 진폭 행렬 [N, n_sub]).

    ESP-IDF CSI 원시값은 서브캐리어마다 [허수, 실수] 순서의 2개 값이다.
    first_word == 1 이면 앞 4바이트(서브캐리어 2개)가 무효라서 NaN으로 처리한다.
    """
    df = pd.read_csv(path)
    raw = [parse_values(s) for s in df["data"]]
    n = min(len(r) for r in raw)  # 길이가 섞여 있으면 짧은 쪽에 맞춤
    iq = np.stack([r[:n] for r in raw])
    imag, real = iq[:, 0::2], iq[:, 1::2]
    amp = np.sqrt(imag**2 + real**2)
    if drop_invalid_first_word and "first_word" in df:
        bad = df["first_word"].to_numpy() == 1
        amp[bad, :2] = np.nan
    df = df.drop(columns=["data"])
    df["t"] = df["pc_time"] - df["pc_time"].iloc[0]
    return df, amp


def quicklook(path: str | Path, show: bool = False) -> Path:
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df, amp = load_csi(path)
    t = df["t"].to_numpy()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    ax1.imshow(amp.T, aspect="auto", origin="lower",
               extent=[t[0], t[-1], 0, amp.shape[1]], cmap="viridis")
    ax1.set_ylabel("subcarrier")
    ax1.set_title(f"CSI amplitude — {Path(path).name}")

    # 라벨 구간 표시 + 움직임 지표(서브캐리어 평균 진폭의 1초 이동 표준편차)
    mean_amp = np.nanmean(amp, axis=1)
    hz = len(t) / max(t[-1] - t[0], 1e-6)
    motion = pd.Series(mean_amp).rolling(max(int(hz), 1), min_periods=1).std()
    ax2.plot(t, motion, lw=0.8)
    ax2.set_ylabel("motion (1s std)")
    ax2.set_xlabel("time [s]")
    for label, g in df.groupby((df["label"] != df["label"].shift()).cumsum()):
        ax2.axvspan(g["t"].iloc[0], g["t"].iloc[-1], alpha=0.08,
                    color="C1" if label % 2 else "C2")
        ax2.text(g["t"].iloc[0], ax2.get_ylim()[1] * 0.9, g["label"].iloc[0], fontsize=8)

    fig.tight_layout()
    out = Path(path).with_suffix(".png")
    fig.savefig(out, dpi=120)
    if show:
        plt.show()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    print("저장:", quicklook(a.csv, a.show))