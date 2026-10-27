# RX — CSI 수신기

> 담당: 팀원 · 기반: esp-csi `examples/get-started/csi_recv` · 상태: **수신 확인과 Hz·손실률 측정 전**

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

## 출력 형식 (RX → PC 인터페이스)

```
type,id,mac,rssi,rate,...,len,first_word,data              ← 헤더, 첫 패킷 전 1회
CSI_DATA,1234,1a:00:00:00:00:00,-45,11,...,len,0,"[v0,v1,...]"
```

- ESP32 long format은 25필드이고, C5/C6 short format은 15필드입니다.
- `id`: 로거가 빈 번호로 손실률을 계산합니다.
- `data`: 서브캐리어마다 **`[허수, 실수]`** 순서의 정수 2개입니다.
- `first_word == 1`이면 앞 서브캐리어 2개가 무효입니다. [`pc/csi_io.py`](../../pc/csi_io.py)가 NaN으로 처리합니다.
- 로거는 헤더 줄로 필드를 매핑하고, 헤더를 못 받으면 필드 개수로 형식을 추정합니다.
  → **필드를 추가하면 헤더도 반드시 같이 출력**합니다. (`rx_id` 추가 여부는 아직 미정)

## 구현 규칙

- CSI 콜백 안에서 직접 출력하지 않습니다. **큐에 넣은 뒤 별도 태스크에서 출력**합니다.
- `CSI_DATA` 줄 중간에 다른 로그가 섞이지 않게 합니다. 섞이면 로거에서 파싱 실패로 집계됩니다.
- 보레이트는 921600bps이며, `csi_logger.py --baud`의 기본값과 같습니다. 이 속도로 100Hz 텍스트 출력이 충분한지는 **아직 측정하지 않았습니다**. 손실이 크면 보레이트 상향, 데이터 축소, 바이너리 출력, UDP 중에서 검토합니다. 바이너리 출력이나 UDP로 바꾸면 PC 로거도 함께 수정해야 합니다.

## 빌드 · 플래시

`IDF_v5.5.6_Powershell` 터미널에서 실행합니다.

```powershell
cd C:\esp\wifi-sensing\firmware\rx
idf.py set-target esp32            # TX 보드는 ESP32로 확인됨. RX 보드도 같은지 확인할 것
idf.py build
idf.py -p COMx erase-flash         # 새 보드 최초 1회
idf.py -p COMx flash monitor       # CSI_DATA 줄이 흐르는지 확인 → Ctrl+]로 종료
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
- 로거는 5초마다 수신 Hz, 손실률, RSSI를 출력합니다. 첫 측정 결과는 CLAUDE.md의 진행 상태에 기록합니다.
- `data\`는 git에서 제외됩니다. RX 1대당 시간당 약 200MB입니다.

## 배치

- RX1: 침대가 TX와 RX1 사이에 오도록 둡니다. TX–RX 거리는 2~5m, 높이는 0.7~1.2m입니다.
- RX2: 출입문 쪽에 두고 출입 이벤트를 감지합니다(보드 추가 도착 후). 여러 RX의 데이터는 `id` 기준으로 정렬합니다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 부팅 로그만 나오고 `CSI_DATA`가 없음 | TX 동작 여부, 채널·MAC 일치 |
| 로거가 포트를 못 엶 | `idf.py monitor` 등 다른 프로그램이 포트를 쓰는 중 |
| 로거의 `파싱 실패`가 계속 늘어남 | 보레이트 불일치, `CSI_DATA` 줄에 다른 로그가 섞임 |
| 수신 Hz가 TX 주기보다 낮음 | 시리얼 대역폭(위 "구현 규칙" 참고), 무선 혼잡, TX–RX 거리 |
