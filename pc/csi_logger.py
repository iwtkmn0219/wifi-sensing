"""
CSI 로거 — esp-csi `csi_recv` 시리얼 출력을 CSV로 저장하고, 시나리오 라벨을 붙인다.

사용 예
    # 실제 보드 (idf.py monitor는 반드시 종료한 뒤 실행)
    python csi_logger.py --port COM5
    python csi_logger.py --port COM5 COM6 --baud 921600      # RX 여러 대

    # 보드 없이 테스트 (가짜 CSI 생성, 0.25Hz 호흡 신호 포함)
    python csi_logger.py --simulate

실행 중 콘솔 입력
    라벨 이름 + Enter : 현재 라벨 변경 (예: empty, sit, lying, walk)
    Enter만           : 현재 상태 출력
    q + Enter         : 종료

출력 (logs/<세션시각>/)
    <rx>.csv      : 패킷 1개 = 1행. pc_time, label, rx + 보드가 보낸 필드 전부
    events.csv    : 라벨 변경 시각 기록
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# esp-csi csi_recv 출력 형식
#   - 첫 패킷 직전에 헤더 줄 1회 출력 ("type,id,mac,...")
#   - 이후 "CSI_DATA,..." 형태로 패킷마다 1줄, 마지막 data는 "[v0,v1,...]" (따옴표 포함)
#   헤더를 받으면 그 이름을 쓰고, 못 받으면(로거를 늦게 켠 경우) 필드 개수로 추정한다.
#   → 팀원이 필드를 추가해도 헤더만 출력하면 자동으로 따라간다.
# ---------------------------------------------------------------------------
LONG_FMT = (  # ESP32 / S3 / C3
    "type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,"
    "stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,"
    "ant,sig_len,rx_format,len,first_word,data"
).split(",")
SHORT_FMT = (  # C5 / C6 / C61
    "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,"
    "sig_len,rx_format,len,first_word,data"
).split(",")
DEFAULT_FORMATS = {len(LONG_FMT): LONG_FMT, len(SHORT_FMT): SHORT_FMT}
SEQ_KEYS = ("seq", "id")  # TX의 count가 들어 있는 필드 (예제는 'id')


class Shared:
    """스레드 간 공유 상태 (현재 라벨, 종료 플래그)."""

    def __init__(self) -> None:
        self.label = "unlabeled"
        self.stop = threading.Event()
        self.lock = threading.Lock()


class Stats:
    """주기적으로 출력할 수신 통계."""

    def __init__(self) -> None:
        self.reset_window()
        self.total = 0
        self.lost_total = 0
        self.last_seq: int | None = None
        self.csi_len = None

    def reset_window(self) -> None:
        self.win_start = time.time()
        self.win_count = 0
        self.win_lost = 0
        self.win_rssi = 0
        self.parse_errors = 0
        self.seq_resets = 0

    def update(self, seq: int | None, rssi: int | None, csi_len: int | None) -> None:
        self.total += 1
        self.win_count += 1
        if rssi is not None:
            self.win_rssi += rssi
        if csi_len is not None:
            self.csi_len = csi_len
        if seq is not None:
            if self.last_seq is not None:
                gap = seq - self.last_seq
                if gap > 1:
                    self.win_lost += gap - 1
                    self.lost_total += gap - 1
                elif gap <= 0:  # TX 재부팅 등으로 카운터가 처음부터 다시 시작
                    self.seq_resets += 1
            self.last_seq = seq

    def summary(self, name: str) -> str:
        dt = max(time.time() - self.win_start, 1e-6)
        hz = self.win_count / dt
        expected = self.win_count + self.win_lost
        loss = (self.win_lost / expected * 100) if expected else 0.0
        rssi = (self.win_rssi / self.win_count) if self.win_count else float("nan")
        msg = (
            f"[{name}] {hz:6.1f} Hz | 손실 {loss:5.1f}% | RSSI {rssi:6.1f} dBm | "
            f"CSI len {self.csi_len} | 누적 {self.total}"
        )
        if self.parse_errors:
            msg += f" | 파싱 실패 {self.parse_errors}"
        if self.seq_resets:
            msg += f" | seq 리셋 {self.seq_resets}"
        return msg


def to_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class RxWorker(threading.Thread):
    """RX 하나(시리얼 포트 하나 또는 시뮬레이터)를 읽어서 CSV로 저장한다."""

    def __init__(self, name: str, line_source, out_dir: Path, shared: Shared) -> None:
        super().__init__(daemon=True, name=name)
        self.rx_name = name
        self.lines = line_source
        self.shared = shared
        self.stats = Stats()
        self.columns: list[str] | None = None
        self.path = out_dir / f"{safe_name(name)}.csv"
        self.file = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self.header_written: list[str] | None = None

    def run(self) -> None:
        try:
            for raw in self.lines:
                if self.shared.stop.is_set():
                    break
                self.handle_line(raw.strip())
        except Exception as exc:  # 포트 분리 등
            print(f"\n[{self.rx_name}] 읽기 중단: {exc}", file=sys.stderr)
        finally:
            self.file.flush()
            self.file.close()

    def handle_line(self, line: str) -> None:
        if not line:
            return
        if line.startswith("type,"):  # 보드가 보낸 헤더
            self.columns = line.split(",")
            return
        if not line.startswith("CSI_DATA"):
            return  # 부팅 로그 등은 무시

        try:
            fields = next(csv.reader([line]))
        except Exception:
            self.stats.parse_errors += 1
            return

        cols = self.columns if self.columns and len(self.columns) == len(fields) else None
        cols = cols or DEFAULT_FORMATS.get(len(fields))
        if cols is None:
            self.stats.parse_errors += 1
            return

        row = dict(zip(cols, fields))
        row.pop("type", None)

        seq = next((to_int(row[k]) for k in SEQ_KEYS if k in row), None)
        data = row.get("data", "")
        n_values = data.count(",") + 1 if data.startswith("[") else None
        self.stats.update(seq, to_int(row.get("rssi")), n_values)

        with self.shared.lock:
            label = self.shared.label

        keys = list(row.keys())
        if self.header_written != keys:  # 첫 행(또는 형식 변경 시) 헤더 기록
            self.writer.writerow(["pc_time", "label", "rx", *keys])
            self.header_written = keys
        self.writer.writerow([f"{time.time():.6f}", label, self.rx_name, *row.values()])


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


# ---------------------------------------------------------------------------
# 입력 소스
# ---------------------------------------------------------------------------
def serial_lines(port: str, baud: int, shared: Shared):
    try:
        import serial  # pyserial
    except ImportError:
        sys.exit("pyserial이 필요합니다:  pip install pyserial")

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 1
    # 포트를 열 때 DTR/RTS 때문에 보드가 리셋되거나 다운로드 모드로 빠지는 것을 방지
    ser.dtr = False
    ser.rts = False
    ser.open()
    print(f"[{port}] 연결됨 ({baud} bps)")
    buf = b""
    try:
        while not shared.stop.is_set():
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", errors="ignore")
    finally:
        ser.close()


def simulated_lines(shared: Shared, hz: float = 100.0, n_sub: int = 64, drop: float = 0.01):
    """csi_recv(ESP32 long format)를 흉내 낸 가짜 데이터.
    호흡(0.25Hz)은 항상 약하게, 라벨에 'walk'가 들어가면 큰 움직임 잡음을 섞는다."""
    rng = random.Random(0)
    base = [rng.uniform(10, 30) for _ in range(n_sub)]
    phase = [rng.uniform(0, 2 * math.pi) for _ in range(n_sub)]
    yield ",".join(LONG_FMT)
    t0 = time.time()
    seq = 0
    next_t = t0
    while not shared.stop.is_set():
        next_t += 1.0 / hz
        time.sleep(max(0.0, next_t - time.time()))
        seq += 1
        if rng.random() < drop:
            continue  # 패킷 손실 흉내
        t = time.time() - t0
        with shared.lock:
            label = shared.label
        motion = 6.0 if "walk" in label else (0.0 if "empty" in label else 0.3)
        breath = 0.0 if "empty" in label else 1.5 * math.sin(2 * math.pi * 0.25 * t)
        vals = []
        for k in range(n_sub):
            amp = base[k] + breath * (k % 3 == 0) + rng.gauss(0, 0.5 + motion)
            amp = max(amp, 0.0)
            vals += [int(amp * math.sin(phase[k])), int(amp * math.cos(phase[k]))]  # imag, real
        meta = [seq, "1a:00:00:00:00:00", int(-45 + rng.gauss(0, 2)), 11, 1, 0, 0, 0, 0, 0,
                0, 0, 0, -95, 0, 11, 0, int(t * 1e6), 0, 47, 1, len(vals), 0]
        yield "CSI_DATA," + ",".join(map(str, meta)) + ',"[' + ",".join(map(str, vals)) + ']"'


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="ESP32 CSI 시리얼 로거")
    ap.add_argument("--port", nargs="+", help="시리얼 포트 (여러 개 가능, 예: COM5 COM6)")
    ap.add_argument("--baud", type=int, default=921600, help="보레이트 (RX 펌웨어 설정과 동일하게)")
    ap.add_argument("--out", default="logs", help="저장 폴더")
    ap.add_argument("--label", default="unlabeled", help="시작 라벨")
    ap.add_argument("--stats", type=float, default=5.0, help="통계 출력 주기(초)")
    ap.add_argument("--simulate", action="store_true", help="보드 없이 가짜 데이터로 실행")
    args = ap.parse_args()

    if not args.port and not args.simulate:
        ap.error("--port 또는 --simulate 중 하나는 필요합니다")

    shared = Shared()
    shared.label = args.label
    session = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) / session
    out_dir.mkdir(parents=True, exist_ok=True)

    events_f = open(out_dir / "events.csv", "w", newline="", encoding="utf-8")
    events = csv.writer(events_f)
    events.writerow(["pc_time", "label"])
    events.writerow([f"{time.time():.6f}", shared.label])

    workers: list[RxWorker] = []
    if args.simulate:
        workers.append(RxWorker("sim", simulated_lines(shared), out_dir, shared))
    for port in args.port or []:
        workers.append(RxWorker(port, serial_lines(port, args.baud, shared), out_dir, shared))
    for w in workers:
        w.start()

    def report() -> None:
        while not shared.stop.wait(args.stats):
            with shared.lock:
                label = shared.label
            for w in workers:
                print(f"  ({label}) {w.stats.summary(w.rx_name)}")
                w.stats.reset_window()

    threading.Thread(target=report, daemon=True).start()
    print(f"저장 위치: {out_dir.resolve()}")
    print("라벨 입력 후 Enter로 변경 / Enter만: 상태 / q: 종료\n")

    try:
        while True:
            cmd = input().strip()
            if cmd.lower() in ("q", "quit", "exit"):
                break
            if not cmd:
                for w in workers:
                    print("  " + w.stats.summary(w.rx_name))
                continue
            with shared.lock:
                shared.label = cmd
            events.writerow([f"{time.time():.6f}", cmd])
            events_f.flush()
            print(f"  → 라벨: {cmd}")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        shared.stop.set()
        for w in workers:
            w.join(timeout=2)
        events_f.close()
        print("\n종료. 저장된 파일:")
        for w in workers:
            print(f"  {w.path}  ({w.stats.total} 패킷, 손실 {w.stats.lost_total})")


if __name__ == "__main__":
    main()