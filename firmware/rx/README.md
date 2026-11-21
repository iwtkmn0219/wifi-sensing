# RX — CSI 수신기

> 담당: 팀원 · 기반: esp-csi `examples/get-started/csi_recv` · 상태: **921600 + TX 50Hz 확정 — 2분 실측에서 시리얼 손상 0, `queue_drop` 0 (2026-11-13)**

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
| 방식 | ESP-NOW 브로드캐스트, **50Hz** |
| RX 시리얼 | **921600bps** (TX 50Hz와 짝. 100Hz면 2M 필요) |
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
- `STATS` 줄은 로거가 세션 폴더의 `<포트>_stats.csv`에 따로 저장합니다(`rx_stats.py`가 함께 요약). `idf.py monitor`로도 볼 수 있습니다.
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
cd E:\esp\wifi-sensing\firmware\rx
idf.py set-target esp32            # TX 보드는 ESP32로 확인됨. RX 보드도 같은지 확인할 것
idf.py build
idf.py -p COMx erase-flash         # 새 보드 최초 1회
idf.py -p COMx flash monitor       # CSI_DATA·STATS 줄 확인 → Ctrl+]로 종료
```

부팅 로그:

```
csi_rx: own_mac=30:76:f5:..:..:..  filter_tx_mac=1a:00:00:00:00:00
csi_rx: channel=11  bw=HT40-  lltf_only=0  baud=921600
```

## 보레이트 / 출력량

**현재 설정: 921600bps + TX 50Hz** (2026-11-13). 전체 CSI 한 줄 약 1.3KB × 50Hz ≈ 0.66Mbps = 921600의 약 71%(계산값).

| 설정 | 대역폭 | 실측 |
|---|---|---|
| 921600 + 91~100Hz | 부족 (약 1.2~1.3Mbps 필요) | 저장률 71.5Hz에서 막힘, CSI 행 손상 0.08% |
| 2M + 91~100Hz | 충분 (`queue_drop` 0) | **PC 수신 바이트 손실로 CSI 행 약 11% 손상** |
| **921600 + 50Hz (현재)** | 약 71% (`queue_drop` 0) | **CSI 행 손상 0/5773**, 저장률 48.1Hz (TX 50Hz의 96%) |

- **보레이트 위치** — `sdkconfig`와 `sdkconfig.defaults` 둘 다 `CONFIG_ESP_CONSOLE_UART_BAUDRATE`. `sdkconfig`도 커밋되어 있으므로 값을 바꿀 때는 두 파일을 같이 고칩니다. `idf.py monitor`는 자동으로 따라갑니다(`CONFIG_ESPTOOLPY_MONITOR_BAUD`는 콘솔 보레이트에서 파생되는 숨은 옵션, `build/project_description.json`의 `monitor_baud`로 확인). 로거 기본값도 921600입니다(다른 보레이트는 `--baud`로 지정).
- **100Hz로 되돌릴 때** — TX `CONFIG_SEND_FREQUENCY` 100, RX 보레이트 2000000, 로거 `--baud 2000000`을 함께 바꿉니다. 2M 바이트 손실은 로거가 손상 행을 걸러내지만, 숫자 하나가 다른 숫자로 바뀐 손상은 못 잡으므로 줄 체크섬이나 (b)를 검토합니다.
- **(b) LLTF만 출력** — `idf.py menuconfig` → **CSI RX Config** → `CSI 앞 128바이트(LLTF, 64 서브캐리어)만 출력`. `len`이 128이 되며 출력량이 약 1/3. CSI 설정 자체는 그대로. **PC 분석은 64 서브캐리어 기준이 됨.**

menuconfig → **CSI RX Config** 기본값: 채널 11 / HT40 y / LLTF만 n / 큐 32 / STATS 5초

## 측정 기록

| 날짜 | 펌웨어 | 보레이트 | 시간 | 수신 Hz | 간격 median / mean / std | 손실률(id) | RSSI 평균 (min/max) | CSI len |
|---|---|---|---|---|---|---|---|---|
| 10/27 | csi_recv 예제 | 921600 | 119.5 s | 68.9 | 11.02 / 14.63 / 17.38 ms (>20ms 1541회, max 825ms) | 24.79 % | −61.0 (−80/−55) dBm | 384 B (192 sc) |
| 10/27 | firmware/rx | 921600 | 153.2 s | 71.5 | 11.56 / 13.99 / 5.97 ms (>20ms 2204회, max 56.8ms) | 21.34 % | −50.1 (−80/−44) dBm | 384 B (192 sc) |
| 11/13 | firmware/rx | 2000000 | 177.3 s | 64.4 | 11.48 / 15.52 / 8.94 ms (>20ms 2981회, max 112.6ms) | 28.69 % (STATS) | −64.7 (−90/−59) dBm | 384 B (192 sc) |
| 11/13 ② | firmware/rx (`620cc56`) + 새 로거 | 2000000 | 59.0 s | 85.4 (보드) / 75.7 (저장) | 11.01 / 13.20 / 5.73 ms (>20ms 698회, max 55.3ms) | **6.05 % (STATS)** | −53.9 dBm | 384 B (192 sc) |
| 11/13 ③ | firmware/rx + **TX 100Hz(`7b5357f`)** | 2000000 | 177.8 s | 93.7 (보드) / 82.9 (저장) | 10.01 / 12.06 / 5.19 ms (>20ms 1465회, max 62.6ms) | **6.31 % (STATS)** | −50.8 dBm | 384 B (192 sc) |
| 11/13 ④ | firmware/rx + **TX 50Hz (`52de6f7`)** | **921600** | 119.9 s | 48.2 (보드) / 48.1 (저장) | 20.00 / 20.78 / 4.44 ms (max 79.9ms) | **3.65 % (STATS)** | −49.8 (−69/−45) dBm | 384 B (192 sc) |

- 10/27 해석(추정): 한 줄 약 1.3KB × 69줄/s ≈ 921600bps 실효 전송량(약 92KB/s)과 일치 → **시리얼 병목**. 예제는 콜백 안에서 출력하므로 출력이 막히는 동안 수신이 누락됨. median 11.02ms → TX 실제 주기 약 91Hz(`usleep` 방식).
- 10/27 firmware/rx 921600: 큐 분리로 max 간격은 825ms → 56.8ms로 줄었지만 저장률 71.5Hz·손실 21.34%로 여전히 시리얼 병목으로 추정 → **2M으로 변경**.
- 11/13 firmware/rx 2M (COM4, 세션 `20261113_141024`, TX는 다른 PC에서 전원):
  - **`queue_drop` = 0 (STATS 36구간 전부)** → RX 내부 시리얼 병목은 해소. 수신 64.4Hz + `seq_lost` ≈ TX 91Hz.
  - **남은 손실 28.69%는 전부 `seq_lost`(무선 구간)**. 간격 분포는 1칸 빠짐 2383회, 2칸 688회 … 로 짧은 손실이 고르게 퍼져 있음(긴 끊김 아님). 원인은 미확정 — 같은 시각 TX `tx_stats`의 on-air Hz, 채널 11 HT40− 간섭, 배치(RSSI가 10/27보다 약 15dB 낮음)를 확인해야 함.
  - **PC 수신 쪽에서 바이트가 빠짐**: CSI 배열 손상 행 1313/11584(**11.3%**, 값 개수 ≠ `len` 992행 + 숫자 하나가 통째로 빠져 `-`·빈칸이 된 행 321행), 헤더 글자 손상 3회(`channl`, `channelsecondary_channel`), `id` 숫자 손상 1회(163516 → 16516). 당시 로거는 손상된 헤더를 열 이름으로 받아 CSV 중간에 헤더를 다시 썼음 → 11/13 로거·`rx_stats.py`·`csi_io.py`에서 손상 행을 거르도록 수정. 손실률은 보드 STATS 값을 기준으로 함.
  - 921600(10/27)은 손상 9/10956(0.08%)이고 한 번에 수백 값씩 빠짐(버퍼 넘침 모양), 2M은 1바이트씩 자주 빠짐(UART 수신 단계 추정). 클럭 분주는 ESP32 80MHz/2M=40, CH340 12MHz/2M=6으로 둘 다 정수라 클럭 오차는 원인이 아님(이론). CH340 최대 속도의 여유 부족·케이블을 의심(추정).
- 11/13 ② 보드 재연결 후 새 펌웨어·새 로거 검증 (60초): `queue_drop` 0, **무선 손실 6.05%**(①의 28.69%에서 감소, RSSI −64.7 → −53.9dBm). 보드를 다시 놓으면서 위치가 바뀐 것으로 보이며, 무선 손실은 배치·RSSI 영향이 큰 것으로 추정. 시리얼 손상 행 564개(약 11%)는 로거가 저장하지 않음 → 로거 기준 손실 16.62% = 무선 6% + 시리얼 손상 약 11%. CSV의 RSSI 최대값 −5는 `−55`에서 숫자 하나가 빠진 손상으로 보이며, 메타데이터 숫자 손상은 형식 검사로 못 잡음(줄 체크섬 필요).
- 11/13 ③ TX `vTaskDelayUntil`(100Hz) 적용 확인 (세션 `20261113_174751`): `id`·보드 시각으로 계산한 TX 송신 **100.00Hz, id 1칸 간격 median 10.00ms**(이전 세션 17:29·17:41은 90.90Hz·11.00ms이고 그 사이 `id` 리셋이 없어 TX가 재플래시되지 않은 상태였음). 100Hz에서도 `queue_drop` 0, 무선 손실 6.31%(대부분 2~5%, 1670~1700초 구간 약 30초 동안 13~21%로 상승 — 원인 미확인). 시리얼 손상 행 1862개(약 11%)로 91Hz 때와 같은 비율.
- 11/13 ④ **50Hz + 921600 확정** (세션 `20261113_181550`, 브랜치 `rate-50hz-921600`): TX 송신 **50.00Hz, id 1칸 간격 median 20.00ms**, 2분간 `id` 감소 0회(재부팅·다른 TX 없음). `queue_drop` 0, 무선 손실 3.65%. **시리얼 손상 행 0/5773, `data` 형식 위반 0, RSSI 범위 −69~−45dBm(이상값 없음)** → 2M의 손상 약 11%가 사라짐. 저장률 48.1Hz로 TX의 96%(100Hz+2M은 83%).
  - 무선 손실이 낮은 것은 배치(RSSI 약 −50dBm) 영향도 있어 송신 주기 효과와 구분되지 않음.
  - `rx_stats.py`의 `>20 ms` 횟수는 100Hz 기준이라 50Hz에서는 의미 없음(정상 간격이 20ms).
  - 100Hz가 다시 필요해지면 남은 후보: LLTF만 출력 / 줄 단위 체크섬(RX·PC 동시) / 2M에서 케이블·포트 교체 재측정.

측정 숫자는 [`pc/rx_stats.py`](../../pc/rx_stats.py)로 계산합니다.

```powershell
cd E:\esp\wifi-sensing\pc
python rx_stats.py ..\data\<세션>\*.csv     # events.csv·*_stats.csv는 자동 제외, 보드 STATS 요약 포함
```

## 데이터 수집

`idf.py monitor`가 포트를 독점하므로 **monitor를 닫은 뒤** 로거를 실행합니다.

```powershell
cd E:\esp\wifi-sensing\pc
pip install -r requirements.txt
python csi_logger.py --port COM5   # 기본 921600, 저장소의 data\<세션>\에 저장. RX 여러 대: --port COM5 COM6
python csi_io.py ..\data\<세션>\COM5_csi.csv           # 히트맵·움직임 그래프 PNG (로거 저장 이름: <포트>_csi.csv)
```

- 실행 중 라벨을 입력하고 Enter를 누르면 이후 패킷에 그 라벨이 붙습니다. 라벨 예: `empty`, `sit`, `lying`, `walk`, `door`, `fan`, `fall`
- 로거는 5초마다 수신 Hz, 손실률, RSSI, 손상 행·손상 헤더 수를 출력합니다. 손상 행은 저장하지 않으므로 로거의 손실률 = 무선 손실 + 시리얼 손상이고, 무선 손실만 보려면 `<포트>_stats.csv`의 `seq_lost`를 봅니다.
- `data\`는 git에서 제외됩니다. RX 1대당 시간당 약 150MB입니다(50Hz, 전체 CSI 기준. 100Hz면 약 300MB).

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
