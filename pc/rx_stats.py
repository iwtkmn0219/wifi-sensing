r"""
csi_logger.py 가 저장한 CSV에서 RX 수신 품질 숫자를 계산한다.
(수신 Hz, 간격 흔들림, 손실률, RSSI, CSI 길이 — 진행 상태 기록용)

사용 예
    python rx_stats.py ..\data\20261027_210000\COM4.csv
    python rx_stats.py ..\data\20261027_210000\*.csv        # RX 여러 대 (events.csv, *_stats.csv 는 자동 제외)

- 간격은 보드 시각(local_timestamp, us)으로 계산한다. PC 수신 시각(pc_time)은 시리얼 버퍼링 때문에 흔들린다.
- 손실률은 id(TX count)의 빈 구간으로 계산한다. id 가 줄어들면(TX 재부팅) 구간을 나눈다.
- 시리얼 바이트 손실 대비 (2M 실측):
    * CSV 중간의 반복 헤더 행, 숫자 열이 숫자가 아닌 행은 버린다 (이전 로거로 저장한 파일)
    * 앞뒤 id 사이에 끼지 않는 id 1개(예: 163516 → 16516)는 손상 id로 보고 손실 계산에서 뺀다
    * data 값 개수 ≠ len 인 행 수를 따로 출력한다
- 같은 폴더에 <rx>_stats.csv(보드 STATS)가 있으면 queue_drop / seq_lost 합계도 출력한다.
  보드 STATS 손실률은 시리얼 손상의 영향을 받지 않으므로 이 값을 우선한다.
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

NUM_COLS = ("pc_time", "id", "rssi", "local_timestamp", "len")


def seq_loss(ids: np.ndarray) -> tuple[float, int]:
    expected = received = 0
    start = 0
    for i in range(1, len(ids) + 1):
        if i == len(ids) or ids[i] < ids[i - 1]:      # 구간 끝 또는 TX 재부팅
            seg = ids[start:i]
            expected += int(seg.max() - seg.min() + 1)
            received += len(np.unique(seg))
            start = i
    return 1 - received / expected, expected - received


def corrupted_ids(ids: np.ndarray, max_gap: int = 1000) -> np.ndarray:
    """앞뒤 id 는 정상적으로 이어지는데 가운데 하나만 튀는 id (True = 손상)."""
    bad = np.zeros(len(ids), bool)
    if len(ids) < 3:
        return bad
    prev, cur, nxt = ids[:-2], ids[1:-1], ids[2:]
    around_ok = (nxt > prev) & (nxt - prev < max_gap)
    bad[1:-1] = around_ok & ((cur <= prev) | (cur >= nxt))
    return bad


def load_rows(path: str) -> tuple[pd.DataFrame, int, int]:
    """(유효 행, 버린 행 수, CSI 값 개수 ≠ len 인 행 수)"""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    n0 = len(df)
    df = df[df["pc_time"] != "pc_time"]
    if "data" in df:  # 필드 개수가 어긋난 행은 data 자리에 다른 값이 들어간다
        df = df[df["data"].str.startswith("[") & df["data"].str.endswith("]")]
    for c in NUM_COLS:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c for c in NUM_COLS if c in df])
    bad_len = 0
    if "data" in df:
        n_vals = df["data"].str.count(",") + 1
        fmt_ok = df["data"].str.fullmatch(r"\[-?\d+(?:,-?\d+)*\]")
        bad_len = int(((n_vals != df["len"]) | ~fmt_ok).sum())
        df = df.drop(columns=["data"])
    return df.reset_index(drop=True), n0 - len(df), bad_len


def board_stats(path: str) -> str | None:
    p = Path(path)
    sp = p.with_name(p.stem + "_stats.csv")
    if not sp.exists():
        return None
    s = pd.read_csv(sp)
    num = {k: pd.to_numeric(s[k], errors="coerce") for k in ("seq_lost", "queue_drop", "rx_hz", "uptime_s")}
    lost, qd, hz = num["seq_lost"].sum(), num["queue_drop"].sum(), num["rx_hz"]
    period = num["uptime_s"].diff().median()  # STATS 주기(초)
    recv = (hz * period).sum()
    loss = 100 * lost / (recv + lost) if recv + lost else float("nan")
    return (f"  board STATS        : {len(s)}구간, seq_lost {int(lost)}, queue_drop {int(qd)}, "
            f"loss {loss:.2f} %, rx_hz 평균 {hz.mean():.1f}")


def report(path: str) -> None:
    name = Path(path).name
    if name == "events.csv" or name.endswith("_stats.csv"):
        return
    try:
        df, dropped, bad_len = load_rows(path)
    except pd.errors.EmptyDataError:  # 포트를 못 열어 빈 파일만 생긴 경우 등
        print(f"== {path}: 빈 파일")
        return
    if df.empty or "local_timestamp" not in df:
        print(f"== {path}: 데이터 없음")
        return
    ts = df["local_timestamp"].to_numpy(np.int64)
    dt = np.diff(ts) % (1 << 32) / 1000.0              # ms, uint32 us 랩어라운드 보정
    dt = dt[(dt > 0) & (dt < 5000)]                     # 재부팅 등 비정상 간격 제외
    dur = dt.sum() / 1000.0
    ids = df["id"].to_numpy(np.int64)
    bad_id = corrupted_ids(ids)
    loss, lost = seq_loss(ids[~bad_id])

    print(f"== {path}  (rx={df['rx'].iloc[0]}) ==")
    print(f"  packets            : {len(df)}  ({dur:.1f} s)")
    print(f"  rx rate            : {len(dt) / dur:.1f} Hz  (median interval {np.median(dt):.2f} ms)")
    print(f"  interval mean/std  : {dt.mean():.2f} / {dt.std():.2f} ms,  >20 ms {int((dt > 20).sum())}회, max {dt.max():.1f} ms")
    print(f"  packet loss (id)   : {100 * loss:.2f} %  ({lost} lost)")
    print(f"  RSSI mean (min/max): {df['rssi'].mean():.1f} dBm ({int(df['rssi'].min())}/{int(df['rssi'].max())})")
    for L, c in df["len"].value_counts().items():
        print(f"  CSI len            : {int(L)} B = {int(L) // 2} subcarriers  x{c}")
    if "label" in df:
        print("  labels             : " + ", ".join(f"{k}({v})" for k, v in df["label"].value_counts().items()))
    if dropped or bad_len or bad_id.any():
        print(f"  serial damage      : 버린 행 {dropped}, CSI 배열 손상(개수≠len·형식) {bad_len}행 "
              f"({100 * bad_len / max(len(df), 1):.2f} %), 손상 id {int(bad_id.sum())}개")
    bs = board_stats(path)
    if bs:
        print(bs)


if __name__ == "__main__":
    paths = [p for a in sys.argv[1:] for p in (glob.glob(a) or [a])]
    if not paths:
        sys.exit(__doc__)
    for p in paths:
        report(p)
