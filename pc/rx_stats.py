r"""
csi_logger.py 가 저장한 CSV에서 RX 수신 품질 숫자를 계산한다.
(수신 Hz, 간격 흔들림, 손실률, RSSI, CSI 길이 — 진행 상태 기록용)

사용 예
    python rx_stats.py ..\data\20261027_210000\COM4.csv
    python rx_stats.py ..\data\20261027_210000\*.csv        # RX 여러 대

- 간격은 보드 시각(local_timestamp, us)으로 계산한다. PC 수신 시각(pc_time)은 시리얼 버퍼링 때문에 흔들린다.
- 손실률은 id(TX count)의 빈 구간으로 계산한다. id 가 줄어들면(TX 재부팅) 구간을 나눈다.
"""
from __future__ import annotations

import glob
import sys

import numpy as np
import pandas as pd


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


def report(path: str) -> None:
    df = pd.read_csv(path, usecols=lambda c: c != "data")
    if df.empty:
        print(f"== {path}: 데이터 없음")
        return
    ts = df["local_timestamp"].to_numpy(np.int64)
    dt = np.diff(ts) % (1 << 32) / 1000.0              # ms, uint32 us 랩어라운드 보정
    dt = dt[(dt > 0) & (dt < 5000)]                     # 재부팅 등 비정상 간격 제외
    dur = dt.sum() / 1000.0
    loss, lost = seq_loss(df["id"].to_numpy(np.int64))

    print(f"== {path}  (rx={df['rx'].iloc[0]}) ==")
    print(f"  packets            : {len(df)}  ({dur:.1f} s)")
    print(f"  rx rate            : {len(dt) / dur:.1f} Hz  (median interval {np.median(dt):.2f} ms)")
    print(f"  interval mean/std  : {dt.mean():.2f} / {dt.std():.2f} ms,  >20 ms {int((dt > 20).sum())}회, max {dt.max():.1f} ms")
    print(f"  packet loss (id)   : {100 * loss:.2f} %  ({lost} lost)")
    print(f"  RSSI mean (min/max): {df['rssi'].mean():.1f} dBm ({df['rssi'].min()}/{df['rssi'].max()})")
    for L, c in df["len"].value_counts().items():
        print(f"  CSI len            : {L} B = {L // 2} subcarriers  x{c}")
    if "label" in df:
        print("  labels             : " + ", ".join(f"{k}({v})" for k, v in df["label"].value_counts().items()))


if __name__ == "__main__":
    paths = [p for a in sys.argv[1:] for p in (glob.glob(a) or [a])]
    if not paths:
        sys.exit(__doc__)
    for p in paths:
        report(p)
