r"""
창 나누기 + 특징 추출 — preprocess.py 출력(processed\<세션>\<RX>.npz)을 창 단위 특징 표로 만든다.

사용 예
    cd E:\esp\wifi-sensing\pc
    python features.py                         # ..\processed 전체, 60초 창 / 30초 간격
    python features.py --win 12 --step 3      # 숨 참기용 짧은 창
    python features.py ..\processed\20261121_170403_hold --win 12 --step 3

출력
    ..\processed\features_win<창>s.csv   창 1개 = 1행 (session, rx, t0, id0, label, cls + 특징)
    화면: 클래스별 창 개수, 특징별 AUC(빈 방 vs 정적 재실, 정적 재실 vs 숨 참기)

창 규칙
    - 창 안의 격자점이 전부 valid이고 클래스가 하나일 때만 사용한다 (경계·공백·클래스 없는 구간이 걸치면 버림)
    - 창 시작은 id 기준이라 같은 세션의 RX끼리 같은 창이 같은 순간이다 (id0로 맞춤)

특징 (모두 창 안에서 계산, A = 노이즈 제거된 진폭 [시간, 서브캐리어])
    periodicity      PC1의 0.1~0.5Hz 최대 피크(±1 bin) 파워 / 0.05~2Hz 전체 (csi_io.periodicity)
    peak_bpm         그 피크의 호흡수 (회/분)
    breath_ratio     PC1의 0.1~0.5Hz 파워 / 0.05~2Hz 파워 (피크 하나가 아니라 대역 전체)
    breath_entropy   0.1~0.5Hz 안 스펙트럼 엔트로피 (0~1, 낮을수록 한 주파수에 몰림)
    acf_peak         PC1 자기상관의 2~10초 지연 구간 최대값 (주기 2~10초 = 분당 6~30회)
    pc1_var_ratio    대역통과 후 PC1이 설명하는 분산 비율 (서브캐리어들이 함께 움직이는 정도)
    breath_std       0.1~0.5Hz 대역 표준편차의 서브캐리어 중앙값 (×1e3)
    motion_std       0.5~2Hz 대역 표준편차의 서브캐리어 중앙값 (×1e3)
    slow_std         0.02~0.1Hz 대역 표준편차의 서브캐리어 중앙값 (×1e3, 느린 자세 변화·드리프트)
    amp_std          대역통과 없이 서브캐리어별 표준편차의 중앙값 (×1e3)
"""
from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import pandas as pd

import csi_io
from preprocess import load_processed

BANDS = {"slow": (0.02, 0.1), "breath": (0.1, 0.5), "motion": (0.5, 2.0)}
FEATURES = ["periodicity", "peak_bpm", "breath_ratio", "breath_entropy", "acf_peak", "pc1_var_ratio",
            "breath_std", "motion_std", "slow_std", "amp_std"]


def band_std(A: np.ndarray, fs: float, lo: float, hi: float) -> float:
    X = np.fft.rfft(A - A.mean(0), axis=0)
    f = np.fft.rfftfreq(len(A), 1 / fs)
    X[(f < lo) | (f > hi)] = 0
    return float(np.median(np.fft.irfft(X, len(A), axis=0).std(0)))


def window_features(W: np.ndarray, fs: float) -> dict:
    score, bpm = csi_io.periodicity(W, fs)
    # PC1 (csi_io.periodicity와 같은 방식)
    B = np.stack([csi_io._bandpass(W[:, j], fs, *csi_io.ANALYSIS_BAND) for j in range(W.shape[1])], axis=1)
    B = (B - B.mean(0)) / (B.std(0) + 1e-9)
    u, s, _ = np.linalg.svd(B, full_matrices=False)
    x = u[:, 0]
    P = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / fs)
    tot = P[(f >= 0.05) & (f <= 2.0)].sum() + 1e-12
    mb = (f >= 0.1) & (f <= 0.5)
    pb = P[mb] / (P[mb].sum() + 1e-12)
    entropy = float(-(pb * np.log(pb + 1e-12)).sum() / np.log(max(mb.sum(), 2)))
    xb = csi_io._bandpass(x, fs, *csi_io.BREATH_BAND)
    ac = np.correlate(xb, xb, "full")[len(xb) - 1:]
    ac = ac / (ac[0] + 1e-12)
    lo, hi = int(2 * fs), min(int(10 * fs), len(ac) - 1)
    return {
        "periodicity": score, "peak_bpm": bpm,
        "breath_ratio": float(P[mb].sum() / tot), "breath_entropy": entropy,
        "acf_peak": float(ac[lo:hi + 1].max()) if hi > lo else np.nan,
        "pc1_var_ratio": float(s[0] ** 2 / (s ** 2).sum()),
        "breath_std": 1e3 * band_std(W, fs, *BANDS["breath"]),
        "motion_std": 1e3 * band_std(W, fs, *BANDS["motion"]),
        "slow_std": 1e3 * band_std(W, fs, *BANDS["slow"]),
        "amp_std": 1e3 * float(np.median(W.std(0))),
    }


def windows_of(d: dict, win: float, step: float) -> list[dict]:
    fs = float(d["meta"]["fs_out"])
    n, k = int(round(win * fs)), int(round(step * fs))
    ids, seg, valid, cls, lab = d["id"], d["segment"], d["valid"], d["cls"], d["label"]
    id_step = int(np.median(np.diff(ids[seg == seg[0]]))) if len(ids) > 1 else 1
    rows = []
    for sg in np.unique(seg):
        idx = np.where(seg == sg)[0]
        if len(idx) < n:
            continue
        # 창 시작을 id 기준으로 맞춘다 (id가 step 격자의 배수인 지점부터) → RX끼리 같은 창
        first = idx[0]
        id_win = n * id_step
        id_hop = k * id_step
        start_id = int(np.ceil(ids[first] / id_hop) * id_hop)
        pos = {int(v): i for i, v in zip(idx, ids[idx])}
        sid = start_id
        while True:
            i0 = pos.get(sid)
            if i0 is None:
                if sid > ids[idx[-1]]:
                    break
                sid += id_hop
                continue
            i1 = i0 + n
            if i1 > idx[-1] + 1:
                break
            w_valid, w_cls = valid[i0:i1], cls[i0:i1]
            if w_valid.all() and (w_cls == w_cls[0]).all() and w_cls[0] != "":
                feats = window_features(d["amp"][i0:i1].astype(float), fs)
                rows.append({"session": d["meta"]["session"], "rx": d["meta"]["rx"], "t0": float(d["t"][i0]),
                             "id0": int(ids[i0]), "win": win, "label": str(lab[i0]), "cls": str(w_cls[0]), **feats})
            sid += id_hop
    return rows


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """Mann-Whitney U로 계산한 AUC (pos가 neg보다 클 확률). 0.5 = 구분 못 함, 1 또는 0 = 완전 구분"""
    if len(pos) == 0 or len(neg) == 0:
        return np.nan
    r = pd.Series(np.r_[pos, neg]).rank().to_numpy()
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def report(df: pd.DataFrame) -> None:
    print("\n클래스별 창 개수 (RX별)")
    print(df.pivot_table(index="rx", columns="cls", values="t0", aggfunc="size", fill_value=0).to_string())
    for a, b in (("static", "empty"), ("static", "hold")):
        sub = df[df.cls.isin([a, b])]
        if sub.cls.nunique() < 2:
            continue
        print(f"\nAUC: {a} vs {b}  (1에 가까울수록 {a}가 큼, 0에 가까울수록 {b}가 큼, 0.5 = 구분 못 함)")
        both = [s for s, g in sub.groupby("session") if g.cls.nunique() == 2]
        if both:
            print(f"  같은 세션 안에 두 클래스가 모두 있는 세션: {', '.join(both)}")
        else:
            print(f"  ⚠ 두 클래스가 서로 다른 세션에서 왔습니다 ({a}: {', '.join(sorted(sub[sub.cls == a].session.unique()))} / "
                  f"{b}: {', '.join(sorted(sub[sub.cls == b].session.unique()))}).\n"
                  f"    AUC에 클래스 차이와 세션(배치·환경·시간) 차이가 섞여 있어 그대로 믿으면 안 됩니다.")
        out = {}
        for rx, g in sub.groupby("rx"):
            if g.cls.nunique() < 2:
                continue
            out[rx] = {f: auc(g.loc[g.cls == a, f].to_numpy(), g.loc[g.cls == b, f].to_numpy()) for f in FEATURES}
            out[rx]["(창 수)"] = f"{(g.cls == a).sum()}/{(g.cls == b).sum()}"
        if out:
            print(pd.DataFrame(out).to_string(float_format=lambda v: f"{v:.2f}"))


def main() -> None:
    root = Path(__file__).resolve().parent.parent / "processed"
    ap = argparse.ArgumentParser(description="창 나누기 + 특징 추출")
    ap.add_argument("inputs", nargs="*", default=[str(root)], help="processed 폴더, 세션 폴더 또는 .npz (기본: ..\\processed 전체)")
    ap.add_argument("--win", type=float, default=60.0, help="창 길이(초)")
    ap.add_argument("--step", type=float, default=None, help="창 간격(초, 기본 창 길이의 절반)")
    ap.add_argument("--out", default=None, help="출력 CSV (기본 ..\\processed\\features_win<창>s.csv)")
    a = ap.parse_args()
    step = a.step or a.win / 2

    files = []
    for inp in a.inputs:
        for p in (glob.glob(inp) or [inp]):
            p = Path(p)
            files += sorted(p.rglob("*.npz")) if p.is_dir() else [p] if p.suffix == ".npz" else []
    if not files:
        raise SystemExit("npz 파일이 없습니다. 먼저 preprocess.py를 실행하세요")

    rows = []
    for f in files:
        d = load_processed(f)
        r = windows_of(d, a.win, step)
        print(f"{d['meta']['session']}/{d['meta']['rx']}: 창 {len(r)}개")
        rows += r
    df = pd.DataFrame(rows)
    out = Path(a.out) if a.out else root / f"features_win{a.win:g}s.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n저장: {out} ({len(df)}행)")
    if not df.empty:
        report(df)


if __name__ == "__main__":
    main()
