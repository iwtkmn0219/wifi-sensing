"""
저장된 CSI CSV를 불러와 진폭 행렬로 바꾸고, 빠르게 눈으로 확인하는 도구.

사용 예
    python csi_io.py ../data/20261027_210000/COM5_csi.csv    # 히트맵 PNG 저장
    python csi_io.py ../data/.../sim.csv --show               # 창으로 보기
    python csi_io.py ../data/.../COM4_csi.csv --breath        # 라벨별 호흡 주기성 점수 (60초 창)
    python csi_io.py ../data/.../COM4_csi.csv --breath 30 --edge 30   # 30초 창, 라벨 경계 앞뒤 30초 제외

코드에서
    from csi_io import load_csi, subcarrier_segments, null_subcarriers
    df, amp = load_csi("../data/.../COM5_csi.csv")   # amp: (패킷 수, 서브캐리어 수)
    segs = subcarrier_segments(amp.shape[1])      # {'lltf': slice(0, 64), 'htltf': slice(64, 192)}
    keep = ~null_subcarriers(amp)                 # 가드·DC처럼 항상 0인 서브캐리어 제외

    from csi_io import preprocess, breathing_scores
    grid, A = preprocess(df, amp)                 # 이득 정규화 → 10Hz 재샘플 → Hampel 이상치 제거
    scores = breathing_scores(grid, A, df)        # 창별 주기성 점수·피크 호흡수·라벨

노이즈 제거 (2026-11-21 실측: COM4 빈 방 `20261121_155026` vs 누워서 호흡 `20261121_163812`)
    breathing_scores(win=60, edge=30) 기준       분리도   호흡 창 중 빈 방 상위 10% 초과
    원본 진폭 (gain_norm=False, outlier=False)   −0.04     0%
    preprocess() 기본값 (이득 정규화 + Hampel)    +0.08    35%   (우연이면 10%)
    (분리도 = (호흡 중앙값 − 빈 방 중앙값) / 빈 방 IQR. 호흡 창이 17개뿐이라 창 위치에 따라 흔들린다.
     별도 스크립트에서는 이득 정규화가 가장 효과가 컸고, 0.5초 이동평균은 오히려 나빠져서 기본값에서 뺐다.)
    아직 판별기로 쓸 수준은 아니며, 당시 COM4 RSSI가 −82dBm으로 낮았다. 신호 세기를 올린 배치에서 다시 확인 필요.
"""

from __future__ import annotations

import argparse
import warnings
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
    """가드 밴드·DC처럼 거의 항상 0인 서브캐리어 마스크 (True = 제외 대상). 전부 NaN인 열도 제외."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # first_word 무효로 전부 NaN인 열
        med = np.nanmedian(amp, axis=0)
    return np.nan_to_num(med, nan=0.0) < thresh


# ---------------------------------------------------------------------------
# 노이즈 제거와 호흡 주기성
# ---------------------------------------------------------------------------
BREATH_BAND = (0.1, 0.5)   # Hz, 분당 6~30회
ANALYSIS_BAND = (0.05, 2.0)


def normalize_gain(amp: np.ndarray) -> np.ndarray:
    """패킷마다 서브캐리어 진폭 벡터를 L2 크기로 나눈다.

    ESP32는 수신 이득(AGC)을 보정하지 않아서 패킷마다 전체 진폭이 같이 오르내린다.
    호흡은 서브캐리어마다 다르게 영향을 주므로(주파수 선택적), 공통 이득을 나눠도 남는다.
    """
    norm = np.sqrt(np.nansum(amp**2, axis=1, keepdims=True))
    return amp / (norm + 1e-9)


def resample(t: np.ndarray, amp: np.ndarray, fs: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    """불규칙한 수신 시각(손실 포함)을 fs Hz 등간격 격자로 선형 보간한다."""
    grid = np.arange(t[0], t[-1], 1 / fs)
    A = np.stack([np.interp(grid, t, amp[:, j]) for j in range(amp.shape[1])], axis=1)
    return grid, A


def hampel(A: np.ndarray, half_window: int = 10, n_sigma: float = 3.0) -> np.ndarray:
    """열(서브캐리어)마다 이동 중앙값에서 n_sigma×MAD 넘게 튄 값을 중앙값으로 바꾼다."""
    s = pd.DataFrame(A)
    w = 2 * half_window + 1
    med = s.rolling(w, center=True, min_periods=1).median()
    mad = (s - med).abs().rolling(w, center=True, min_periods=1).median() * 1.4826
    return s.where((s - med).abs() <= n_sigma * mad + 1e-9, med).to_numpy()


def preprocess(df: pd.DataFrame, amp: np.ndarray, fs: float = 10.0,
               gain_norm: bool = True, outlier: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """load_csi 결과 → (시각 격자[pc_time], 노이즈 제거된 진폭 [len(grid), 유효 서브캐리어]).

    순서: null 서브캐리어·앞 2개(first_word 무효 가능) 제외 → 이득 정규화 → fs Hz 재샘플 → Hampel.
    """
    keep = ~null_subcarriers(amp)
    keep[:2] = False
    A = np.nan_to_num(amp[:, keep].astype(float))
    if gain_norm:
        A = normalize_gain(A)
    grid, A = resample(df["pc_time"].to_numpy(), A, fs)
    if outlier:
        A = hampel(A, half_window=int(fs))
    return grid, A


def _bandpass(x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
    X = np.fft.rfft(x - x.mean())
    f = np.fft.rfftfreq(len(x), 1 / fs)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, len(x))


def periodicity(window: np.ndarray, fs: float = 10.0) -> tuple[float, float]:
    """한 창 [샘플, 서브캐리어] → (주기성 점수 0~1, 피크 호흡수 회/분).

    서브캐리어별 0.05~2Hz 대역통과·표준화 → PCA 1차 성분 → 0.1~0.5Hz 최대 피크(±1 bin) 파워 / 0.05~2Hz 전체 파워.
    규칙적인 호흡이면 한 주파수에 파워가 몰려 점수가 높고, 잡음이면 퍼져서 낮다.
    """
    B = np.stack([_bandpass(window[:, j], fs, *ANALYSIS_BAND) for j in range(window.shape[1])], axis=1)
    B = (B - B.mean(0)) / (B.std(0) + 1e-9)
    u, _, _ = np.linalg.svd(B, full_matrices=False)
    x = u[:, 0]
    P = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / fs)
    total = P[(f >= ANALYSIS_BAND[0]) & (f <= ANALYSIS_BAND[1])].sum() + 1e-12
    m = np.where((f >= BREATH_BAND[0]) & (f <= BREATH_BAND[1]))[0]
    i = m[np.argmax(P[m])]
    return float(P[max(i - 1, 0):i + 2].sum() / total), float(60 * f[i])


def breathing_scores(grid: np.ndarray, A: np.ndarray, df: pd.DataFrame | None = None,
                     win: float = 60.0, step: float | None = None, fs: float = 10.0,
                     edge: float = 0.0) -> pd.DataFrame:
    """창마다 주기성 점수·피크 호흡수를 계산한다.

    df(load_csi 결과)를 주면 각 창에 라벨을 붙이고, 라벨이 바뀌는 창은 'mixed'로 표시한다.
    edge > 0 이면 라벨 구간 경계에서 edge초 안쪽에 걸친 창도 'mixed'로 둔다(라벨 입력 지연·이동 구간 제외).
    """
    step = step or win / 2
    n, k = int(win * fs), int(step * fs)
    labels = None
    if df is not None and "label" in df:
        idx = np.clip(np.searchsorted(df["pc_time"].to_numpy(), grid), 0, len(df) - 1)
        labels = df["label"].to_numpy()[idx]
        if edge > 0:
            # 라벨이 바뀌는 시각 + 기록 시작·끝에서 edge초 안쪽은 제외
            ct = np.r_[grid[0], grid[1:][labels[1:] != labels[:-1]], grid[-1]]
            pos = np.clip(np.searchsorted(ct, grid), 1, len(ct) - 1)
            dist = np.minimum(np.abs(grid - ct[pos - 1]), np.abs(ct[pos] - grid))
            labels = np.where(dist < edge, "mixed", labels.astype(object))
    rows = []
    for s0 in range(0, len(grid) - n + 1, k):
        sc, bpm = periodicity(A[s0:s0 + n], fs)
        lab = None
        if labels is not None:
            w = labels[s0:s0 + n]
            lab = w[0] if (w == w[0]).all() else "mixed"
        rows.append({"t0": grid[s0], "label": lab, "score": sc, "bpm": bpm})
    return pd.DataFrame(rows)


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


def breath_report(path: str | Path, win: float = 60.0, edge: float = 0.0) -> pd.DataFrame:
    """라벨별 주기성 점수 요약을 출력하고 창별 결과를 돌려준다."""
    df, amp = load_csi(path)
    grid, A = preprocess(df, amp)
    sc = breathing_scores(grid, A, df, win=win, edge=edge)
    print(f"{Path(path).name}: 창 {win:.0f}s, 경계 제외 {edge:.0f}s (점수가 높을수록 규칙적인 호흡 주기)")
    if sc.empty or (sc["label"] == "mixed").all():
        print(f"  계산할 창이 없습니다 (기록 {grid[-1] - grid[0]:.0f}s, 라벨 구간이 창 {win:.0f}s보다 짧거나 경계 제외로 모두 빠짐)")
        return sc
    sc = sc[sc["label"] != "mixed"]
    summary = sc.groupby("label").agg(
        windows=("score", "size"),
        score_median=("score", "median"),
        score_p25=("score", lambda s: s.quantile(0.25)),
        score_p75=("score", lambda s: s.quantile(0.75)),
        bpm_median=("bpm", "median"),
    )
    print(summary.round(3).to_string())
    return sc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--breath", nargs="?", const=60.0, type=float, metavar="WIN",
                    help="퀵룩 대신 라벨별 호흡 주기성 점수 출력 (창 길이 초, 기본 60)")
    ap.add_argument("--edge", type=float, default=0.0, help="--breath: 라벨 경계 앞뒤 제외 초")
    a = ap.parse_args()
    if a.breath:
        breath_report(a.csv, a.breath, a.edge)
    else:
        print("저장:", quicklook(a.csv, a.show))