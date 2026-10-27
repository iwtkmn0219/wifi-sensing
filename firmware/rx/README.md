# RX — CSI 수신기

> 담당: 팀원 · 기반: esp-csi `examples/get-started/csi_recv` · 상태: **펌웨어 작성·빌드 완료, 보드 실측 전**

TX가 보낸 ESP-NOW 패킷에서 CSI를 추출하고, 패킷 1개당 1줄씩 시리얼로 출력합니다.
출력은 PC의 [`pc/csi_logger.py`](../../pc/csi_logger.py)가 받아서 CSV로 저장합니다.

```
[TX] ──ESP-NOW 브로드캐스트──▶ [RX] ──시리얼(CSI_DATA)──▶ [PC: pc/csi_logger.py]
```

## TX ↔ RX 계약 (변경 시 양쪽 동시 수정)

| 항목 | 값 |
|---|---|
| 채널 | 11 (`CONFIG_LESS_INTERFERENCE_CHANNEL`) |
| 수신 필터 MAC | `1a:00:00:00:00:00` (TX가 `esp_wifi_set_mac`으로 고정한 값) |
| 방식 | ESP-NOW 브로드캐스트, 100Hz |
| 속도 | MCS0 LGI, phymode HT40 |
| `id` 필드 | TX 페이로드 `uint32_t count` (시퀀스 번호) |
| 절전 | `WIFI_PS_NONE` |

전체 내용은 [`firmware/tx/README.md`](../tx/README.md)에 있습니다.

## csi_recv 예제와 다른 점 (출력 형식은 동일)

| | csi_recv 예제 | 이 펌웨어 |
|---|---|---|
| 출력 위치 | CSI 콜백 안에서 `ets_printf` | 콜백은 복사 + 큐 전송만, **별도 태스크에서 출력** |
| 헤더 줄 | 첫 패킷 때 1회 | 부팅 때 1회 + **5초마다 다시 출력** (로거를 늦게 켜도 필드 매핑됨) |
| 손실 진단 | 없음 | **`STATS` 줄** (아래) |
| `id` 추출 | 검사 없이 `payload+15` | ESP-NOW 프레임(category 127)일 때만 추출 |
| RX 자신의 MAC | `1a:00:00:00:00:00`으로 덮어씀 | 보드 고유 MAC 유지 (부팅 로그에 출력) |
| 의존성 | `esp_csi_gain_ctrl` (Component Registry) | 없음 (ESP32는 gain control 미사용, 예제와 동작 동일) |

Wi-Fi·ESP-NOW·CSI 설정(`esp_wifi_set_csi_config`: LLTF, HT-LTF, STBC, ltf_merge, channel_filter 모두 on)은 예제 기본값 그대로입니다. 바꿀 때는 이 README에 이유를 함께 기록합니다.

## 출력 형식 (RX → PC 인터페이스)

```
type,id,mac,rssi,rate,...,len,first_word,data              ← 헤더 (부팅 + 5초마다)
CSI_DATA,1234,1a:00:00:00:00:00,-45,11,...,384,1,"[v0,v1,...]"
STATS,uptime_s=10,rx_hz=90.2,seq_lost=0,loss_pct=0.00,queue_drop=0,bad_payload=0,rssi_avg=-61.0,free_heap=...
```

- `CSI_DATA` 줄은 csi_recv와 **필드·순서가 같은 ESP32 long format(25필드)** 입니다. `csi_logger.py`, `csi_io.py`를 수정 없이 씁니다.
- `data`: 서브캐리어마다 **`[허수, 실수]`** 순서의 정수 2개입니다. `first_word == 1`이면 앞 서브캐리어 2개가 무효입니다.
- `STATS` 줄은 로거가 무시합니다(`CSI_DATA`·`type,`로 시작하지 않는 줄). `idf.py monitor`로 볼 때 진단용입니다.
- `rx_id`는 추가하지 않았습니다. 로거가 포트 이름을 `rx` 열에 넣으므로 PC 1대로 RX 여러 대를 받을 때는 구분됩니다. (RX를 여러 PC에 나눠 꽂거나 UDP로 바꾸면 그때 다시 검토)

### STATS로 손실 원인 구분

| STATS | 의미 | 대응 |
|---|---|---|
| `seq_lost` ↑, `queue_drop` = 0 | **무선 구간 손실** (공중에서 유실, TX 미송신) | 채널 변경(TX/RX 동시), 배치·거리, TX 주기 방식 |
| `queue_drop` ↑ | **시리얼 병목** (출력이 수신을 못 따라감) | 아래 "보레이트 / 출력량" |
| `bad_payload` ↑ | TX MAC에서 온 ESP-NOW가 아닌 프레임 | 보통 0. 계속 늘면 공유 |

## 빌드 · 플래시

`IDF_v5.5.6_Powershell` 터미널에서 실행합니다.

```powershell
cd C:\esp\wifi-sensing\firmware\rx
idf.py set-target esp32            # 보드: ESP32-D0WD-V3 (esp32 확정)
idf.py build
idf.py -p COMx erase-flash         # 새 보드 최초 1회
idf.py -p COMx flash monitor       # CSI_DATA·STATS 줄 확인 → Ctrl+]로 종료
```

부팅 로그:

```
csi_rx: own_mac=30:76:f5:..:..:..  filter_tx_mac=1a:00:00:00:00:00
csi_rx: channel=11  bw=HT40-  lltf_only=0  baud=921600
```

## 보레이트 / 출력량 (921600bps 병목 대응)

csi_recv 예제 실측(아래)에서 921600bps가 병목으로 추정됩니다. `queue_drop`이 계속 생기면 다음 중 하나를 씁니다.

- **(a) 보레이트 2M** — `sdkconfig.defaults`의 `CONFIG_ESP_CONSOLE_UART_BAUDRATE=2000000` → `sdkconfig` 삭제 후 다시 빌드. 로거는 `--baud 2000000`. **PC 쪽 명령도 바뀌므로 공유 필요.** CH340은 최대 2Mbps.
- **(b) LLTF만 출력** — `idf.py menuconfig` → **CSI RX Config** → `CSI 앞 128바이트(LLTF, 64 서브캐리어)만 출력`. `len`이 128이 되며 출력량이 약 1/3. CSI 설정 자체는 그대로. **PC 분석은 64 서브캐리어 기준이 됨.**

menuconfig → **CSI RX Config** 기본값: 채널 11 / HT40 y / LLTF만 n / 큐 32 / STATS 5초

## 측정 기록

| 날짜 | 펌웨어 | 보레이트 | 시간 | 수신 Hz | 간격 median / mean / std | 손실률(id) | RSSI 평균 (min/max) | CSI len |
|---|---|---|---|---|---|---|---|---|
| 10/27 | csi_recv 예제 | 921600 | 119.5 s | 68.9 | 11.02 / 14.63 / 17.38 ms (>20ms 1541회, max 825ms) | 24.79 % | −61.0 (−80/−55) dBm | 384 B (192 sc) |

- 10/27 해석(추정): 한 줄 약 1.3KB × 69줄/s ≈ 921600bps 실효 전송량(약 92KB/s)과 일치 → **시리얼 병목**. 예제는 콜백 안에서 출력하므로 출력이 막히는 동안 수신이 누락됨. median 11.02ms → TX 실제 주기 약 91Hz(`usleep` 방식).
- 다음 측정: 이 펌웨어 921600 → `queue_drop` 확인 → 필요 시 2M.

측정 숫자는 [`pc/rx_stats.py`](../../pc/rx_stats.py)로 계산합니다.

```powershell
cd C:\esp\wifi-sensing\pc
python rx_stats.py ..\data\<세션>\COM4.csv
```

## 데이터 수집

`idf.py monitor`가 포트를 독점하므로 **monitor를 닫은 뒤** 로거를 실행합니다.

```powershell
cd C:\esp\wifi-sensing\pc
pip install -r requirements.txt
python csi_logger.py --port COM5 --out ..\data         # RX 여러 대: --port COM5 COM6
python csi_io.py ..\data\<세션>\COM5.csv               # 히트맵·움직임 그래프 PNG
```

- 실행 중 라벨을 입력하고 Enter를 누르면 이후 패킷에 그 라벨이 붙습니다. 라벨 예: `empty`, `sit`, `lying`, `walk`, `door`, `fan`, `fall`
- 로거는 5초마다 수신 Hz, 손실률, RSSI를 출력합니다.
- `data\`는 git에서 제외됩니다. RX 1대당 시간당 약 200MB입니다.

## 배치

- RX1: 침대가 TX와 RX1 사이에 오도록 둡니다. TX–RX 거리는 2~5m, 높이는 0.7~1.2m입니다.
- RX2: 출입문 쪽에 두고 출입 이벤트를 감지합니다(보드 추가 도착 후). 여러 RX의 데이터는 `id` 기준으로 정렬합니다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 부팅 로그만 나오고 `CSI_DATA`가 없음 | TX 동작 여부, 채널·MAC·HT40 일치 |
| `erase-flash`/`flash` 중 `PermissionError(13)` | 포트를 쓰는 다른 프로그램 종료, PC 본체 USB에 직접 연결, `-b 115200`, 케이블 교체 |
| 로거가 포트를 못 엶 | `idf.py monitor` 등 다른 프로그램이 포트를 쓰는 중 |
| 로거의 `파싱 실패`가 계속 늘어남 | 보레이트 불일치 |
| 수신 Hz가 TX 주기보다 낮음 | `STATS`의 `queue_drop`(시리얼) / `seq_lost`(무선) 중 어느 쪽인지 |
