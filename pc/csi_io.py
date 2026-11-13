"""
저장된 CSI CSV를 불러와 진폭 행렬로 바꾸고, 빠르게 눈으로 확인하는 도구.

사용 예
    python csi_io.py ../data/20261027_210000/COM5.csv        # 히트맵 PNG 저장
    python csi_io.py ../data/.../sim.csv --show               # 창으로 보기

코드에서
    from csi_io import load_csi, subcarrier_segments, null_subcarriers
    df, amp = load_csi("../data/.../COM5.csv")   # amp: (패킷 수, 서브캐리어 수)
    segs = subcarrier_segments(amp.shape[1])      # {'lltf': slice(0, 64), 'htltf': slice(64, 192)}
    keep = ~null_subcarriers(amp)                 # 가드·DC처럼 항상 0인 서브캐리어 제외
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

TEXT_COLS = ("label", "rx", "mac", "data")


def parse_values(s: str) -> np.ndarray:
    """'[v0,v1,...]' → float32 배열. 바이트 손실로 깨진 문자열(예: '1,,2', '1-2')은 빈 배열."""
    try:
        return np.fromstring(s.strip().strip('"').strip("[]"), sep=",", dtype=np.float32)
    except ValueError:
        return np.empty(0, np.float32)


def load_csi(path: str | Path, drop_invalid_first_word: bool = True, verbose: bool = True):
    """CSV → (메타데이터 DataFrame, 진폭 행렬 [N, n_sub]).

    ESP-IDF CSI 원시값은 서브캐리어마다 [허수, 실수] 순서의 2개 값이다.
    first_word == 1 이면 앞 4바이트(서브캐리어 2개)가 무효라서 NaN으로 처리한다.

    손상 행 처리 (2M 시리얼 바이트 손실 대비. 새 로거는 저장 전에 거르지만 이전 파일에도 대응)
      - CSV 중간에 반복된 헤더 행, 숫자 열이 숫자가 아닌 행은 버린다
      - data 값 개수 ≠ len 인 행은 버린다
      - 그 뒤에도 길이가 섞여 있으면 가장 많은 길이만 남긴다 (짧은 쪽으로 자르지 않음)
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    n0 = len(df)
    df = df[df["pc_time"] != "pc_time"]                       # 반복 헤더
    df = df[df["data"].str.startswith("[") & df["data"].str.endswith("]")]  # 필드 개수 어긋난 행
    for c in df.columns:
        if c not in TEXT_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    num_cols = [c for c in ("pc_time", "id", "len", "local_timestamp") if c in df]
    df = df.dropna(subset=num_cols)

    raw = [parse_values(s) for s in df["data"]]
    n_vals = np.array([len(r) for r in raw])
    ok = n_vals == df["len"].to_numpy() if "len" in df else np.ones(len(raw), bool)
    if ok.any():
        common = np.bincount(n_vals[ok]).argmax()
        ok &= n_vals == common
    df = df[ok].reset_index(drop=True)
    raw = [r for r, k in zip(raw, ok) if k]
    if verbose and len(df) < n0:
        print(f"[load_csi] {Path(path).name}: 손상·불일치 행 {n0 - len(df)}/{n0} 제외")
    if not raw:
        raise ValueError(f"{path}: 유효한 CSI 행이 없습니다")

    iq = np.stack(raw)
    imag, real = iq[:, 0::2], iq[:, 1::2]
    amp = np.sqrt(imag**2 + real**2)
    if drop_invalid_first_word and "first_word" in df:
        bad = df["first_word"].to_numpy() == 1
        amp[bad, :2] = np.nan
    df = df.drop(columns=["data"])
    df["t"] = df["pc_time"] - df["pc_time"].iloc[0]
    return df, amp


def subcarrier_segments(n_sub: int) -> dict[str, slice]:
    """ESP32 CSI 버퍼의 구간 (csi_config: lltf + htltf + stbc_htltf2 on 기준).

    64  : LLTF만 (non-HT 패킷 또는 RX의 LLTF만 출력 옵션)
    128 : LLTF 64 + HT-LTF 64  (HT20)
    192 : LLTF 64 + HT-LTF 128 (HT40, 현재 TX·RX 계약)
    그 외 길이는 'all' 하나로 돌려준다. 구간 안의 서브캐리어 순서는 ESP-IDF 문서의 표를 따른다.
    """
    if n_sub == 64:
        return {"lltf": slice(0, 64)}
    if n_sub in (128, 192):
        return {"lltf": slice(0, 64), "htltf": slice(64, n_sub)}
    return {"all": slice(0, n_sub)}


def null_subcarriers(amp: np.ndarray, thresh: float = 0.5) -> np.ndarray:
    """가드 밴드·DC처럼 거의 항상 0인 서브캐리어 마스크 (True = 제외 대상)."""
    return np.nan_to_num(np.nanmedian(amp, axis=0), nan=0.0) < thresh


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
    for name, sl in subcarrier_segments(amp.shape[1]).items():  # 구간 경계와 이름
        if sl.start:
            ax1.axhline(sl.start, color="w", lw=0.8, ls="--")
        ax1.text(t[0], sl.start + 1, name, color="w", fontsize=8, va="bottom")

    # 라벨 구간 표시 + 움직임 지표(유효 서브캐리어 평균 진폭의 1초 이동 표준편차)
    mean_amp = np.nanmean(amp[:, ~null_subcarriers(amp)], axis=1)
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