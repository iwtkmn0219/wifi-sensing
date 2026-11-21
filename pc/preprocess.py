r"""
CSI 세션 전처리 — 로거가 저장한 세션을 모델 입력용 등간격 행렬로 정리한다.

사용 예
    cd E:\esp\wifi-sensing\pc
    python preprocess.py ..\dataset\20261121_163812_breath ..\dataset\20261121_170403_hold
    python preprocess.py ..\data\20261121_155026 --exclude COM5      # 특정 RX 제외
    python preprocess.py ..\data\2026112* --out ..\processed --fs 10 --edge 3

출력 (기본 ..\processed\<세션 폴더 이름>\)
    <RX>.npz       : 아래 배열 (load_processed()로 읽음)
    summary.csv    : (--out 바로 아래) 실행한 RX마다 한 줄 — 품질과 클래스별 유효 시간

단계
  1. 정리      손상 행 제외(csi_io.load_csi), 손상 id 1개짜리 튐 제거(rx_stats.corrupted_ids),
               TX 재부팅(id 감소) 지점에서 구간 분리, 구간 안 중복 id 제거·정렬.
               저장률이 TX 송신률의 절반 미만인 RX는 통째로 제외 (예: 시리얼 손상으로 거의 비어 있는 파일)
  2. 시간축    pc_time 대신 id × TX 주기를 시간으로 쓴다 (TX 50.00Hz 실측, pc_time은 시리얼 버퍼링으로 흔들림).
               출력 격자는 id 기준이라(50Hz→10Hz면 id 5칸마다) 같은 세션의 RX끼리 같은 id로 바로 맞는다.
               빠진 id는 max_gap 초 이하만 보간하고, 더 긴 공백 안의 격자점은 valid=False.
  3. 서브캐리어 0인 가드·DC 서브캐리어와 앞 2개(first_word 무효 가능) 제외. 원래 인덱스는 sc_index에 남김.
  4. 노이즈    패킷별 이득 정규화(normalize_gain) → 격자 보간 → Hampel 이상치 제거 (csi_io 참고, 2026-11-21 효과 확인)
  5. 라벨      이름 통일(LABEL_ALIASES) → 클래스(CLASS_OF). 라벨이 바뀌는 지점·구간 시작·끝에서 edge초 안은 valid=False.
               클래스가 없는 라벨(transition, unlabeled 등)도 valid=False.

npz 배열
    id       int64  [N]      격자 위치의 TX 패킷 번호 (RX끼리 정렬 키)
    t        float  [N]      유닉스 시각(초) = 구간별 (pc_time − id×T) 중앙값 + id×T
    segment  int16  [N]      TX 재부팅으로 나뉜 구간 번호
    amp      float32[N, S]   노이즈 제거된 진폭 (패킷별 L2 정규화 단위)
    valid    bool   [N]      학습·평가에 써도 되는 격자점
    label    str    [N]      통일된 라벨
    cls      str    [N]      클래스 (empty / static / hold / motion / "" = 없음)
    sc_index int16  [S]      amp 열의 원래 서브캐리어 번호 (0~191: LLTF 0~63, HT-LTF 64~191)
    meta     str    []       JSON (세션, RX, TX 주기, 품질 수치, 설정)
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import csi_io
from rx_stats import corrupted_ids

# 라벨 이름 통일 (입력 실수·표기 차이). 소문자·공백 제거 후 적용
LABEL_ALIASES = {
    "lying-free": "lying_free",
    "transition1": "transition",
    "ㅗ": "unlabeled",            # 한/영 키가 한글일 때 'h'
    "": "unlabeled",
}
# 라벨 → 클래스. 여기 없는 라벨은 클래스 없음(valid=False)으로 두고 경고한다
CLASS_OF = {
    "empty": "empty",
    "empty_fan": "empty",
    "breath": "static",
    "lying_free": "static",
    "lying_blanket": "static",
    "lying_side": "static",
    "sit_still": "static",
    "hold": "hold",
    "walk": "motion",
    "door": "motion",
}
for _bpm in (6, 8, 10, 12, 15, 18, 20, 24):           # 메트로놈 호흡 lying_paced12 등
    CLASS_OF[f"lying_paced{_bpm}"] = "static"
NO_CLASS = {"transition", "unlabeled", "check", "dual_check", "board_check", "rate50_921600",
            "tx_modified", "tx_100hz", "tx_reset_test", "rx2m_test"}

RESET_DROP = 100           # id가 이만큼 넘게 줄면 TX 재부팅으로 보고 구간을 나눈다


def normalize_label(s: str) -> str:
    s = str(s).strip().lower().replace(" ", "_")
    return LABEL_ALIASES.get(s, s)


def find_rx_files(session: Path) -> dict[str, Path]:
    """세션 폴더의 RX CSI 파일 {포트: 경로}. 새 이름(COM4_csi.csv[.gz])과 예전 이름(COM4.csv) 모두."""
    found = {}
    for p in sorted(session.iterdir()):
        m = re.fullmatch(r"(.+?)(?:_csi)?\.csv(?:\.gz)?", p.name)
        if not m or p.name == "events.csv" or p.name.endswith(("_stats.csv", "_stats.csv.gz")):
            continue
        port = m.group(1)
        if re.fullmatch(r"(.+?)_csi_\d+", port):    # 열 구성 변경으로 이어진 파일은 아직 지원 안 함
            print(f"  [경고] {p.name}: 이어진 파일은 건너뜀")
            continue
        found.setdefault(port, p)
    return found


def tx_period(df: pd.DataFrame) -> float:
    """id가 1 늘 때 보드 시각(local_timestamp, us)이 얼마나 흐르는지 → TX 주기(초). 정수 Hz로 반올림."""
    ids = df["id"].to_numpy(np.int64)
    ts = df["local_timestamp"].to_numpy(np.int64)
    d_id = np.diff(ids)
    d_ts = (np.diff(ts) % (1 << 32)) / 1e6
    ok = (d_id == 1) & (d_ts > 0) & (d_ts < 1)
    if ok.sum() < 10:
        raise ValueError("TX 주기를 추정할 연속 패킷이 부족합니다")
    return 1.0 / round(1.0 / np.median(d_ts[ok]))


def process_rx(path: Path, fs_out: float = 10.0, max_gap: float = 0.2, edge: float = 3.0,
               min_rate_ratio: float = 0.5) -> dict | None:
    df, amp = csi_io.load_csi(path, verbose=False)
    n_rows = len(df)
    if n_rows < 100:                                  # 시리얼 손상 등으로 거의 비어 있는 파일
        return {"quality": {"rows": int(n_rows), "skipped": f"유효 행 {n_rows}개뿐"}}
    period = tx_period(df)
    fs_tx = 1.0 / period
    step = fs_tx / fs_out
    if abs(step - round(step)) > 1e-6:
        raise ValueError(f"TX {fs_tx:.0f}Hz를 출력 {fs_out}Hz로 나누어떨어지게 줄일 수 없습니다")
    step = int(round(step))

    # --- 1. 정리 ---
    ids = df["id"].to_numpy(np.int64)
    bad = corrupted_ids(ids)
    keep_rows = ~bad
    df, amp, ids = df[keep_rows].reset_index(drop=True), amp[keep_rows], ids[keep_rows]
    seg_of_row = np.cumsum(np.r_[0, np.diff(ids) < -RESET_DROP])

    dur = (df["pc_time"].iloc[-1] - df["pc_time"].iloc[0])
    rate = n_rows / max(dur, 1e-9)
    quality = {
        "rows": int(n_rows), "duration_s": round(float(dur), 1), "rate_hz": round(float(rate), 2),
        "fs_tx": round(float(fs_tx), 2), "corrupted_ids": int(bad.sum()),
        "tx_resets": int(seg_of_row[-1]), "rssi_mean": round(float(df["rssi"].mean()), 1),
    }
    if rate < min_rate_ratio * fs_tx:
        quality["skipped"] = f"저장률 {rate:.1f}Hz < TX {fs_tx:.0f}Hz의 {min_rate_ratio:.0%}"
        return {"quality": quality}

    # --- 3. 서브캐리어 선택 ---
    keep_sc = ~csi_io.null_subcarriers(amp)
    keep_sc[:2] = False
    sc_index = np.where(keep_sc)[0].astype(np.int16)
    A_all = np.nan_to_num(amp[:, keep_sc].astype(float))
    # --- 4a. 패킷별 이득 정규화 (보간 전에) ---
    A_all = csi_io.normalize_gain(A_all)

    labels_all = np.array([normalize_label(s) for s in df["label"]], dtype=object)
    pc_all = df["pc_time"].to_numpy(float)

    out = {k: [] for k in ("id", "t", "segment", "amp", "valid", "label")}
    lost = expected = 0
    for seg in np.unique(seg_of_row):
        r = seg_of_row == seg
        s_ids, order = np.unique(ids[r], return_index=True)       # 중복 제거 + 정렬
        s_A = A_all[r][order]
        s_lab = labels_all[r][order]
        s_pc = pc_all[r][order]
        if len(s_ids) < 2 * step:
            continue
        expected += int(s_ids[-1] - s_ids[0] + 1)
        lost += int(s_ids[-1] - s_ids[0] + 1 - len(s_ids))

        # --- 2. 시간축: id 격자 ---
        g = np.arange(int(np.ceil(s_ids[0] / step)) * step, s_ids[-1] + 1, step, dtype=np.int64)
        t0 = np.median(s_pc - s_ids * period)
        G = np.stack([np.interp(g, s_ids, s_A[:, j]) for j in range(s_A.shape[1])], axis=1)
        # 격자점을 감싸는 실제 수신 id 간격이 max_gap보다 길면 무효
        pos = np.clip(np.searchsorted(s_ids, g), 1, len(s_ids) - 1)
        exact = s_ids[np.clip(np.searchsorted(s_ids, g), 0, len(s_ids) - 1)] == g
        gap = (s_ids[pos] - s_ids[pos - 1]) * period
        valid = exact | (gap <= max_gap)
        # 라벨: 그 격자점 직전에 받은 패킷의 라벨
        lab = s_lab[np.clip(np.searchsorted(s_ids, g, side="right") - 1, 0, len(s_ids) - 1)]

        # --- 4b. Hampel ---
        G = csi_io.hampel(G, half_window=int(fs_out))

        # --- 5. 라벨 경계·구간 끝 제외 ---
        tg = t0 + g * period
        if edge > 0:
            change = np.r_[tg[0], tg[1:][lab[1:] != lab[:-1]], tg[-1]]
            p = np.clip(np.searchsorted(change, tg), 1, len(change) - 1)
            near = np.minimum(np.abs(tg - change[p - 1]), np.abs(change[p] - tg)) < edge
            valid &= ~near

        out["id"].append(g)
        out["t"].append(tg)
        out["segment"].append(np.full(len(g), seg, np.int16))
        out["amp"].append(G.astype(np.float32))
        out["valid"].append(valid)
        out["label"].append(lab)

    res = {k: np.concatenate(v) for k, v in out.items()}
    cls = np.array([CLASS_OF.get(l, "") for l in res["label"]], dtype=object)
    unknown = sorted({l for l in res["label"] if l not in CLASS_OF and l not in NO_CLASS})
    res["valid"] &= cls != ""
    res["cls"] = cls
    res["label"] = res["label"].astype(str)
    res["cls"] = res["cls"].astype(str)
    res["sc_index"] = sc_index
    quality.update({
        "loss_pct": round(100 * lost / max(expected, 1), 2),
        "grid_points": int(len(res["id"])), "valid_points": int(res["valid"].sum()),
        "unknown_labels": unknown,
        "valid_min_by_class": {c: round(float(((res["cls"] == c) & res["valid"]).sum() / fs_out / 60), 2)
                               for c in sorted(set(res["cls"]) - {""})},
    })
    res["quality"] = quality
    return res


def save(res: dict, path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **{k: res[k] for k in ("id", "t", "segment", "amp", "valid", "label", "cls", "sc_index")},
                        meta=np.array(json.dumps(meta, ensure_ascii=False)))


def load_processed(path: str | Path) -> dict:
    """preprocess.py 출력(.npz) → dict. meta는 JSON을 풀어서 돌려준다."""
    with np.load(path, allow_pickle=False) as z:
        d = {k: z[k] for k in z.files}
    d["meta"] = json.loads(str(d["meta"]))
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="CSI 세션 전처리 (정리·시간축·서브캐리어·노이즈·라벨)")
    ap.add_argument("sessions", nargs="+", help="세션 폴더 (와일드카드 가능)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "processed"))
    ap.add_argument("--fs", type=float, default=10.0, help="출력 격자 Hz (TX Hz의 약수)")
    ap.add_argument("--max-gap", type=float, default=0.2, help="보간을 허용하는 최대 수신 공백(초)")
    ap.add_argument("--edge", type=float, default=3.0, help="라벨 경계·구간 끝에서 제외할 초")
    ap.add_argument("--exclude", nargs="*", default=[], help="제외할 RX 포트 (예: COM5)")
    a = ap.parse_args()

    sessions = [Path(p) for s in a.sessions for p in (glob.glob(s) or [s]) if Path(p).is_dir()]
    if not sessions:
        sys.exit("세션 폴더가 없습니다")
    out_root = Path(a.out)
    rows = []
    for sess in sessions:
        print(f"== {sess.name}")
        for port, path in find_rx_files(sess).items():
            if port in a.exclude:
                print(f"  {port}: --exclude로 제외")
                continue
            try:
                res = process_rx(path, a.fs, a.max_gap, a.edge)
            except Exception as exc:                    # 한 RX가 실패해도 나머지는 진행
                print(f"  {port}: 실패 — {exc}")
                rows.append({"session": sess.name, "rx": port, "status": f"실패: {exc}"})
                continue
            q = res["quality"]
            if "skipped" in q:
                print(f"  {port}: 제외 — {q['skipped']}")
                rows.append({"session": sess.name, "rx": port, "status": "제외: " + q["skipped"], **q})
                continue
            meta = {"session": sess.name, "rx": port, "source": str(path), "fs_out": a.fs,
                    "max_gap": a.max_gap, "edge": a.edge, "quality": q}
            dst = out_root / sess.name / f"{port}.npz"
            save(res, dst, meta)
            by_cls = ", ".join(f"{c} {m}분" for c, m in q["valid_min_by_class"].items()) or "유효 클래스 없음"
            warn = f" | 모르는 라벨 {q['unknown_labels']}" if q["unknown_labels"] else ""
            print(f"  {port}: {q['rate_hz']}Hz 저장, 손실 {q['loss_pct']}%, RSSI {q['rssi_mean']} → "
                  f"격자 {q['grid_points']}점(유효 {q['valid_points']}) | {by_cls}{warn}")
            rows.append({"session": sess.name, "rx": port, "status": "ok", "file": str(dst),
                         **{k: v for k, v in q.items() if k != "valid_min_by_class"},
                         **{f"min_{c}": m for c, m in q["valid_min_by_class"].items()}})
    if rows:
        out_root.mkdir(parents=True, exist_ok=True)
        summ = out_root / "summary.csv"
        new = pd.DataFrame(rows)
        if summ.exists():                               # 같은 세션·RX는 최신 결과로 교체
            old = pd.read_csv(summ)
            old = old[~old.set_index(["session", "rx"]).index.isin(new.set_index(["session", "rx"]).index)]
            new = pd.concat([old, new], ignore_index=True)
        new.sort_values(["session", "rx"]).to_csv(summ, index=False, encoding="utf-8-sig")
        print(f"\n요약: {summ}")


if __name__ == "__main__":
    main()
