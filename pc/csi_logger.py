"""
CSI 로거 — esp-csi `csi_recv` 시리얼 출력을 CSV로 저장하고, 시나리오 라벨을 붙인다.

사용 예
    # 실제 보드 (idf.py monitor는 반드시 종료한 뒤 실행). 기본 보레이트 921600 = firmware/rx
    python csi_logger.py --port COM5
    python csi_logger.py --port COM5 COM6                    # RX 여러 대
    python csi_logger.py --port COM5 --baud 2000000          # RX를 2M으로 빌드한 경우

    # 보드 없이 테스트 (가짜 CSI 생성, 0.25Hz 호흡 신호 포함)
    python csi_logger.py --simulate

실행 중 콘솔 입력
    라벨 이름 + Enter : 현재 라벨 변경 (예: empty, sit, lying, walk)
    Enter만           : 현재 상태 출력
    q + Enter         : 종료

출력 (저장소의 data/<세션시각>/, --out으로 변경 가능. data/는 git 제외)
    <rx>_csi.csv   : 패킷 1개 = 1행. pc_time, label, rx + 보드가 보낸 필드 전부 (검증 통과 행만)
    <rx>_csi_2.csv : 수집 중 열 구성이 바뀌었을 때(펌웨어 교체 등) 이어지는 파일
    <rx>_stats.csv : RX의 STATS 줄 (queue_drop, seq_lost 등). 원문은 raw 열
    (파일 이름을 COM4.csv로 하지 않는 이유: "COM4."로 시작하는 이름은 Windows 예약 장치 이름이라 git 등이 읽지 못함)
    events.csv     : 라벨 변경 시각 기록

손상 처리 (시리얼 바이트 손실 대비, 자세한 규칙은 RxWorker 참고)
    값 개수 ≠ len 등 깨진 행은 저장하지 않고 '손상 행'으로 센다. 깨진 헤더는 무시한다.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import re
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

# 실행 위치와 무관하게 저장소 루트의 data/ 에 저장한다 (pc/ 의 상위 폴더)
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data"


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
        self.bad_rows = 0     # 값 개수 ≠ len 등 손상 행 (저장 안 함)
        self.bad_headers = 0  # 손상된 헤더 줄 (무시함)
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
        if self.bad_rows:
            msg += f" | 손상 행 {self.bad_rows}"
        if self.bad_headers:
            msg += f" | 손상 헤더 {self.bad_headers}"
        if self.seq_resets:
            msg += f" | seq 리셋 {self.seq_resets}"
        return msg


def to_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


STATS_KEYS = ("uptime_s", "rx_hz", "seq_lost", "loss_pct", "queue_drop",
              "bad_payload", "rssi_avg", "free_heap")  # firmware/rx STATS 줄


class RxWorker(threading.Thread):
    """RX 하나(시리얼 포트 하나 또는 시뮬레이터)를 읽어서 CSV로 저장한다.

    시리얼 바이트 손실(2M에서 실측)에 대비한 검증
      - 헤더: 알려진 형식이면 바로 사용, 모르는 형식이면 같은 헤더를 2번 연속 받아야 사용
              (보드가 5초마다 헤더를 다시 보내므로, 글자가 깨진 헤더는 연속으로 같게 나오지 않음)
      - 행: 숫자 필드가 숫자가 아니거나, data 값 개수 ≠ len 이면 저장하지 않고 '손상 행'으로 센다
      - 열 구성이 바뀌면(펌웨어 교체 등) 같은 파일에 헤더를 섞지 않고 <rx>_csi_2.csv 로 새 파일을 연다
      - STATS 줄은 <rx>_stats.csv 에 따로 저장한다
    """

    def __init__(self, name: str, line_source, out_dir: Path, shared: Shared) -> None:
        super().__init__(daemon=True, name=name)
        self.rx_name = name
        self.lines = line_source
        self.shared = shared
        self.stats = Stats()
        self.out_dir = out_dir
        self.columns: list[str] | None = None
        self.header_candidate: list[str] | None = None
        self.part = 1
        self.path = out_dir / f"{safe_name(name)}_csi.csv"
        self.file = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self.header_written: list[str] | None = None
        self.bad_rows_total = 0
        self.stats_path = out_dir / f"{safe_name(name)}_stats.csv"
        self.stats_file = None  # STATS 줄이 처음 올 때 연다 (시뮬레이터는 STATS 없음)
        self.stats_writer = None

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
            if self.stats_file:
                self.stats_file.close()

    def handle_line(self, line: str) -> None:
        if not line:
            return
        if line.startswith("type,"):  # 보드가 보낸 헤더
            self.handle_header(line.split(","))
            return
        if line.startswith("STATS,"):
            self.handle_stats(line)
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

        if not row_is_valid(row):
            self.stats.bad_rows += 1
            self.bad_rows_total += 1
            return

        seq = next((to_int(row[k]) for k in SEQ_KEYS if k in row), None)
        n_values = row["data"].count(",") + 1 if "data" in row else None
        self.stats.update(seq, to_int(row.get("rssi")), n_values)

        with self.shared.lock:
            label = self.shared.label

        keys = list(row.keys())
        if self.header_written is None:
            self.writer.writerow(["pc_time", "label", "rx", *keys])
            self.header_written = keys
        elif self.header_written != keys:  # 열 구성 변경 → 새 파일
            self.open_next_part()
            self.writer.writerow(["pc_time", "label", "rx", *keys])
            self.header_written = keys
        self.writer.writerow([f"{time.time():.6f}", label, self.rx_name, *row.values()])

    def handle_header(self, cols: list[str]) -> None:
        if cols in DEFAULT_FORMATS.values() or cols == self.columns:
            self.columns = cols
            self.header_candidate = None
        elif cols == self.header_candidate:  # 모르는 형식이 2번 연속 같게 옴 → 새 형식으로 인정
            self.columns = cols
            self.header_candidate = None
        else:
            self.header_candidate = cols
            self.stats.bad_headers += 1

    def handle_stats(self, line: str) -> None:
        values = {}
        for item in line.split(",")[1:]:
            key, sep, val = item.partition("=")
            if sep and key in STATS_KEYS:
                values[key] = val
        if self.stats_writer is None:
            self.stats_file = open(self.stats_path, "w", newline="", encoding="utf-8")
            self.stats_writer = csv.writer(self.stats_file)
            self.stats_writer.writerow(["pc_time", "rx", *STATS_KEYS, "raw"])
        # 키가 깨진 항목은 빈칸이 되고, 원문은 raw 열에 남는다
        self.stats_writer.writerow([f"{time.time():.6f}", self.rx_name,
                                    *(values.get(k, "") for k in STATS_KEYS), line])
        self.stats_file.flush()

    def open_next_part(self) -> None:
        self.file.close()
        self.part += 1
        self.path = self.out_dir / f"{safe_name(self.rx_name)}_csi_{self.part}.csv"
        self.file = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        print(f"\n[{self.rx_name}] 열 구성이 바뀌어 새 파일로 저장: {self.path.name}")


INT_KEYS = ("id", "seq", "rssi", "len", "first_word", "local_timestamp")
DATA_RE = re.compile(r"\[-?\d+(?:,-?\d+)*\]")


def row_is_valid(row: dict[str, str]) -> bool:
    """시리얼 바이트 손실로 깨진 행을 거른다. (id 숫자가 다른 숫자로 바뀐 경우는 못 잡음)"""
    for k in INT_KEYS:
        if k in row and to_int(row[k]) is None:
            return False
    data = row.get("data")
    if data is None:
        return True
    if not DATA_RE.fullmatch(data):  # 괄호·쉼표·정수 형식 (숫자가 통째로 빠져 '-'나 빈칸이 된 경우 포함)
        return False
    n_values = data.count(",") + 1
    n_len = to_int(row.get("len"))
    return n_len is None or n_values == n_len


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


def simulated_lines(shared: Shared, hz: float = 50.0, n_sub: int = 64, drop: float = 0.01):
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
    ap.add_argument("--baud", type=int, default=921600,
                    help="보레이트 (RX 펌웨어 설정과 동일하게. firmware/rx = 921600)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="저장 폴더 (기본: 저장소의 data/)")
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
            print(f"  {w.path}  ({w.stats.total} 패킷, 손실 {w.stats.lost_total}, "
                  f"손상 행 {w.bad_rows_total})")


if __name__ == "__main__":
    main()