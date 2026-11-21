# CLAUDE.md — wifi-sensing

ESP32 Wi-Fi CSI로 원룸 안의 **움직임·재실·호흡**을 비접촉으로 감지하는 고독사 감지 PoC.
목표는 가능성 검증과 향후 방향 제시이며, 결과는 발표로 정리한다(제품화는 범위 밖).

## 응답 규칙
- 한국어로, 간결하고 직관적으로 설명한다. 앞선 맥락(아래 확정 사항)과 연결해서 답한다.
- 확정 사항(채널, MAC, 출력 형식 등)을 바꾸는 제안은 **영향받는 쪽(TX/RX/PC)을 함께 명시**한다.
- 추측과 실측을 구분한다. 성능 수치는 실제 로그에서 나온 것만 사실로 다룬다.

## 핵심 과제
"빈 방"과 "가만히 누운 사람"을 구분하는 것. 구분 근거는 **호흡(0.1~0.5Hz)의 유무**다.
상태 머신: `외출` / `활동` / `정적 재실(호흡 O)` / `이상 의심(재실 + 움직임·호흡 X, N분)` → `긴급 알림`
- 심박은 ESP32로 어려워 확장 목표로 둔다.
- 반려동물과 2인 이상은 범위에서 제외한다.

## 디렉터리
```
E:\esp\
├─ esp-csi\            # Espressif 공식 저장소. 참고 전용, 절대 수정하지 않음
└─ wifi-sensing\       # 이 저장소
   ├─ firmware\tx\     # csi_send 복사본, 여기서 빌드 (담당: 민준)
   ├─ firmware\rx\     # csi_recv 기반 (담당: 팀원)
   ├─ pc\              # csi_logger.py, csi_io.py, 분석 코드
   ├─ data\            # 수집 로그 (git 제외, RX 1대당 약 150MB/시간, 50Hz 기준)
   ├─ dataset\         # 공유용으로 고른 세션 (gzip, git 포함·공개). 목록과 추가 방법은 dataset\README.md
   ├─ processed\       # pc\preprocess.py 출력 (세션\RX.npz + summary.csv, git 제외, 다시 만들 수 있음)
   └─ docs\            # 인수인계 문서
```

## 환경
- Windows, **ESP-IDF v5.5.6** (EIM으로 설치). 터미널은 `IDF_v5.5.6_Powershell`에서만 `idf.py`가 동작한다.
- 경로에 한글·공백·괄호를 쓰지 않는다 (`E:\esp` 아래만 사용. ESP-IDF는 `E:\esp\v5.5.6\esp-idf`, 도구는 `C:\Espressif\tools`).
- 타겟은 `esp32`다. TX 보드(COM4, CH340)는 부팅 로그로 **ESP32 rev v3.1, 플래시 4MB**임을 확인했다(2026-10-27). RX 보드 모델은 팀원이 확인해야 한다.
- v6.x로 올리지 않는다. 예제와 esp-radar 호환을 위해 v5.4 이상, v6 미만을 유지한다.

## 자주 쓰는 명령
```powershell
# 펌웨어 (firmware\tx 또는 firmware\rx에서)
idf.py set-target esp32
idf.py build
idf.py -p COMx erase-flash        # 새 보드 최초 1회
idf.py -p COMx flash monitor      # 모니터 종료: Ctrl+]

# PC (pc\에서) — 실행 전 idf.py monitor를 반드시 닫을 것 (포트 독점)
pip install -r requirements.txt
python csi_logger.py --simulate                  # 보드 없이 테스트
python csi_logger.py --port COM5                 # 기본 921600·저장소 data\<세션>\. RX 여러 대: --port COM5 COM6
python rx_stats.py ..\data\<세션>\*.csv          # 수신 Hz·손실률·시리얼 손상·보드 STATS 요약
python csi_io.py ..\data\<세션>\COM5_csi.csv     # 히트맵·움직임 그래프 PNG (--breath: 라벨별 호흡 주기성)
python preprocess.py ..\data\<세션> ..\dataset\*  # 전처리 → ..\processed\<세션>\<RX>.npz (id 격자 10Hz, 노이즈 제거, 라벨·클래스·valid)
```

## TX ↔ RX 계약 (변경 시 양쪽 동시 수정)
| 항목 | 값 |
|---|---|
| 채널 | 11 (`CONFIG_LESS_INTERFERENCE_CHANNEL`) |
| 송신 MAC | `1a:00:00:00:00:00` (`esp_wifi_set_mac`으로 고정, 보드 고유 MAC 아님) |
| 방식 | ESP-NOW 브로드캐스트 `FF:FF:FF:FF:FF:FF`, **50Hz** (`CONFIG_SEND_FREQUENCY`, 2026-11-13에 100Hz에서 변경) |
| RX 시리얼 | **921600bps** (`CONFIG_ESP_CONSOLE_UART_BAUDRATE`, PC 로거 `--baud` 기본값과 같음) |
| 속도 | MCS0 LGI, phymode HT40 (`esp_now_set_peer_rate_config`) |
| 페이로드 | `uint32_t count` = 시퀀스 번호 → RX 출력의 `id` 필드 |
| 절전 | `WIFI_PS_NONE` |

- TX는 `csi_send` 예제(esp-csi `8633d67`)를 `firmware\tx`에 복사해 쓴다. `esp-csi\` 안에서 빌드하지 않는다.
- 원본에서 바꾼 것은 두 가지다(계약값 무관).
  - **송신 통계 로그**: 5초마다 `tx_stats: send .. Hz, on-air .. Hz, send_err .., cb_fail ..`를 출력한다.
  - **송신 주기 방식**(2026-11-13): `usleep(10ms)` → `vTaskDelayUntil`(`configTICK_RATE_HZ / CONFIG_SEND_FREQUENCY` tick). 100Hz에서 RX `id`·보드 시각 기준 **100.00Hz, 간격 10.00ms** 실측 확인(세션 `20261113_174751`).
  - **송신 주파수 50Hz**(2026-11-13, 브랜치 `rate-50hz-921600`): RX를 921600으로 낮춰 2M 시리얼 손상(CSI 행 약 11%)을 피하기 위함. RX 기준 **50.00Hz, 간격 20.00ms** 실측 확인(세션 `20261113_181550`).
- 이전 실측(2026-10-27, COM4, `usleep` 방식): 송신 **약 91Hz**(목표 100Hz), send_err·cb_fail 0. 송신 처리 시간이 더해져 주기가 약 11ms가 된 것으로 추정해 방식을 바꿨다.

## RX 출력 → PC 인터페이스
- 헤더 줄(`type,id,mac,...`)을 부팅 때 1회 + 5초마다 다시 출력하고, 패킷마다 `CSI_DATA,...,len,first_word,"[v0,v1,...]"` 1줄을 출력한다. (`firmware/rx`)
- 로거는 RX마다 `<포트>_csi.csv`(CSI)와 `<포트>_stats.csv`를 저장한다. `COM4.csv`처럼 `COM4.`으로 시작하는 이름은 Windows 예약 장치 이름이라 git이 읽지 못해서 쓰지 않는다(2026-11-21 이전 세션은 `COM4.csv`).
- `STATS,...` 줄(5초마다)은 진단용이며 로거가 `<rx>_stats.csv`에 따로 저장한다. `queue_drop`=시리얼 병목, `seq_lost`=무선 손실.
- 로거는 시리얼 손상(2M에서 실측, 921600+50Hz에서는 0)을 거른다: 알려진 형식이 아닌 헤더는 2번 연속 같아야 인정, `data`가 정수 배열 형식이 아니거나 값 개수 ≠ `len`인 행은 저장하지 않음. `id` 숫자가 다른 숫자로 바뀐 손상은 못 잡는다(`rx_stats.py`가 손실 계산에서만 제외).
- ESP32 long format은 25필드이고, C5/C6 short format은 15필드다.
- `csi_logger.py`는 헤더 줄로 필드를 매핑한다. 헤더를 못 받으면 필드 개수로 추정한다. → **필드를 추가하면 헤더도 반드시 같이 출력**한다.
- CSI 원시값은 서브캐리어마다 `[허수, 실수]` 순서다. `first_word == 1`이면 앞 서브캐리어 2개가 무효다.
- 콜백 안에서 직접 출력하지 말고, 큐에 넣은 뒤 별도 태스크에서 출력한다.
- **현재 설정: 50Hz + 921600 (확정).** 전체 CSI(한 줄 약 1.3KB) × 50Hz ≈ 0.66Mbps로 921600의 약 71%(계산값). 2분 실측(2026-11-13): `queue_drop` 0, **CSI 행 손상 0/5773**, 저장률 48.1Hz(TX의 96%), 무선 손실 3.65%.
  - 921600은 91~100Hz 전체 CSI에 부족하다(필요 약 1.2~1.3Mbps, 실측 저장률 71.5Hz에서 막힘). 921600에서 CSI 행 손상은 0.08%.
  - 2M은 대역폭은 충분하지만(`queue_drop` 0) PC 수신에서 바이트가 빠져 CSI 행 약 11%가 손상된다(2026-11-13 실측, `firmware/rx/README.md`).
  - 100Hz로 되돌릴 때는 TX `CONFIG_SEND_FREQUENCY`, RX 보레이트(2M), 로거 `--baud`를 함께 바꾸고 줄 체크섬 또는 LLTF만 출력을 검토한다.
- 데이터 양은 50Hz·전체 CSI 기준 RX 1대당 약 150MB/시간(100Hz 실측 3분 14.8MB의 절반으로 계산).

## 실험 배치
- **침대를 TX와 RX1 사이에** 둔다.
- TX–RX 거리 2~5m, 높이 0.7~1.2m.
- RX2는 출입문 쪽에 둬서 출입 이벤트를 감지한다.
- 라벨 예: `empty`, `sit`, `lying`, `walk`, `door`, `fan`, `fall`
- 호흡의 정답값(ground truth)은 직접 세서 기록한다.
- 평가 지표: 수신 Hz, 손실률(`id` 빈 구간), 재실 판별 정확도, 호흡수 평균 절대오차(분당 횟수), 하루 오탐 횟수, 알림 지연 시간

## 진행 상태
- [x] 구상, ESP-IDF v5.5.6 설치, TX(`csi_send`) 플래시·동작 확인
- [x] PC 로거와 퀵룩 도구 (시뮬레이션으로 검증)
- [x] RX 수신 확인 (csi_recv 예제, 921600): 68.9Hz, 손실 24.79%, RSSI −61dBm, CSI 384B(192 서브캐리어) — 시리얼 병목 추정, TX 실제 주기 약 91Hz
- [x] RX 펌웨어 `firmware/rx` 작성 (큐+출력 태스크, STATS, csi_recv와 같은 출력 형식). 빌드·보드 실측 완료
- [x] TX↔RX 연결 확인 (2026-10-27): TX EN 리셋 시각과 RX `id` 리셋을 대조해 RX가 우리 TX 패킷을 받는 것을 확인
- [x] 2분 연속 측정 (2026-10-27 19:06, 921600, RX 펌웨어는 `firmware/rx`로 추정 — 팀원 확인 필요)
  - RX(`rx_stats.py`): 153.2s, **71.5Hz**, 손실(id) **21.34%**(2972개), RSSI −50.1dBm(−80/−44), 간격 median 11.56ms, CSI 384B
  - 같은 구간 TX `tx_stats`: on-air 평균 **90.9Hz**, 오류·재부팅 0 → 예상 송신 13,926 ≈ RX 수신+손실 13,928. TX는 전부 송신, 손실은 RX 쪽
  - 해석(추정): 921600bps 실효 약 92KB/s ÷ 71.5Hz ≈ 1.29KB/줄 = 측정된 줄 길이 → **시리얼 병목**
- [x] `firmware/rx` 2M 실측 (2026-11-13, 177s): 64.4Hz, **`queue_drop` 0**(RX 내부 시리얼 병목 해소), 무선 손실(`seq_lost`) 28.7%, RSSI −64.7dBm, PC 수신 바이트 손실로 CSI 행 11.3% 손상
- [x] PC 도구 손상 대응 — 로거(헤더·행 검증, STATS 저장, 기본 2M·data\), csi_io(손상 행 제외, LLTF/HT-LTF 구간·null 서브캐리어), rx_stats(반복 헤더·손상 id) — 2026-11-13
- [x] TX 주기 방식을 `vTaskDelayUntil`로 변경 (2026-11-13) — 100Hz에서 RX 기준 100.00Hz·간격 10.00ms 실측, 무선 손실(STATS) 6.31%, `queue_drop` 0
- [x] 50Hz + 921600 적용·확정 (2026-11-13) — TX 50.00Hz·간격 20.00ms, `queue_drop` 0, **CSI 행 손상 0/5773**, 무선 손실 3.65%, 저장률 48.1Hz
- [ ] 무선 손실 원인 조사 — 배치에 따라 28.7%(RSSI −64.7) ~ 3.7~6%(RSSI −50~−54)로 변동. 같은 시각 TX on-air Hz 대조, HT20/HT40·채널·배치 비교 (TX·RX 동시 변경)
- [ ] RX 출력 형식 확정 — 현재 csi_recv long format 그대로, `rx_id`는 로거 `rx`(포트) 열로 대체 중
- [x] 전처리 `pc/preprocess.py` (2026-11-21) — 손상·중복·재부팅 정리, id×TX주기 시간축(RX끼리 같은 id 격자), null 서브캐리어 제외, 이득 정규화+Hampel, 라벨 통일·클래스(empty/static/hold/motion)·경계 제외
  - 라벨 → 클래스 표는 `preprocess.py`의 `CLASS_OF`. 새 라벨을 쓰면 여기에 추가한다(없으면 경고하고 학습에서 제외)
  - RX마다 null 서브캐리어 판정이 달라 열 수가 다를 수 있다(예: 183 vs 181). 두 RX를 함께 쓰려면 `sc_index` 교집합으로 맞춘다
- [ ] 움직임 판별 → 호흡 검출 → 상태 머신 → 알림(텔레그램 등, 이후 AWS IoT Core/Lambda/SNS 확장)
- [ ] 보드 2대 추가 도착 후 RX2(출입문) 구성, 다중 RX를 `id` 기준으로 정렬

## 하지 말 것
- `esp-csi\` 원본 수정
- `data\`, `build\` 커밋 (`sdkconfig`는 커밋해서 설정 공유). 공유할 세션은 `dataset\`에 압축해서 넣는다(공개 저장소이므로 참가자 동의 필수, 개인 식별 정보 금지)
- TX/RX 한쪽만 채널·MAC·대역폭 변경
- 실험 영상 보관 (라벨링 후 폐기, 참가자 동의 필수)
