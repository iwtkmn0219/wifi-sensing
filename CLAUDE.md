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
C:\esp\
├─ esp-csi\            # Espressif 공식 저장소. 참고 전용, 절대 수정하지 않음
└─ wifi-sensing\       # 이 저장소
   ├─ firmware\tx\     # csi_send 기반 (담당: 민준)
   ├─ firmware\rx\     # csi_recv 기반 (담당: 팀원)
   ├─ pc\              # csi_logger.py, csi_io.py, 분석 코드
   ├─ data\            # 수집 로그 (git 제외, RX 1대당 약 200MB/시간)
   └─ docs\            # 인수인계 문서
```

## 환경
- Windows, **ESP-IDF v5.5.6** (EIM으로 설치). 터미널은 `IDF_v5.5.6_Powershell`에서만 `idf.py`가 동작한다.
- 경로에 한글·공백·괄호를 쓰지 않는다 (`C:\esp` 아래만 사용).
- 타겟은 `esp32`. RX 보드에서 `ESP32-D0WD-V3 (rev v3.1)` 확인 (10/27, esptool 로그).
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
python csi_logger.py --port COM5 --out ..\data   # RX 여러 대: --port COM5 COM6
python csi_io.py ..\data\<세션>\COM5.csv         # 히트맵·움직임 그래프 PNG
```

## TX ↔ RX 계약 (변경 시 양쪽 동시 수정)
| 항목 | 값 |
|---|---|
| 채널 | 11 (`CONFIG_LESS_INTERFERENCE_CHANNEL`) |
| 송신 MAC | `1a:00:00:00:00:00` (`esp_wifi_set_mac`으로 고정, 보드 고유 MAC 아님) |
| 방식 | ESP-NOW 브로드캐스트 `FF:FF:FF:FF:FF:FF`, 100Hz |
| 속도 | MCS0 LGI, phymode HT40 (`esp_now_set_peer_rate_config`) |
| 페이로드 | `uint32_t count` = 시퀀스 번호 → RX 출력의 `id` 필드 |
| 절전 | `WIFI_PS_NONE` |

- TX는 현재 `csi_send` 예제를 **수정 없이** 사용 중이다(동작 확인 완료).
- 주의: TX는 `usleep` 방식(송신 후 대기)이라 실제 주기가 100Hz보다 약간 낮을 수 있다. 측정 결과가 나쁘면 `vTaskDelayUntil` 방식으로 바꾼다.

## RX 출력 → PC 인터페이스
- 헤더 줄(`type,id,mac,...`)을 부팅 때 1회 + 5초마다 다시 출력하고, 패킷마다 `CSI_DATA,...,len,first_word,"[v0,v1,...]"` 1줄을 출력한다. (`firmware/rx`)
- `STATS,...` 줄(5초마다)은 진단용이며 로거가 무시한다. `queue_drop`=시리얼 병목, `seq_lost`=무선 손실.
- ESP32 long format은 25필드이고, C5/C6 short format은 15필드다.
- `csi_logger.py`는 헤더 줄로 필드를 매핑한다. 헤더를 못 받으면 필드 개수로 추정한다. → **필드를 추가하면 헤더도 반드시 같이 출력**한다.
- CSI 원시값은 서브캐리어마다 `[허수, 실수]` 순서다. `first_word == 1`이면 앞 서브캐리어 2개가 무효다.
- 콜백 안에서 직접 출력하지 말고, 큐에 넣은 뒤 별도 태스크에서 출력한다.
- 921600bps는 100Hz 텍스트 출력에 부족한 것으로 추정된다(csi_recv 예제 실측 68.9Hz·손실 24.8%, `firmware/rx/README.md` 측정 기록). 다음 단계는 보레이트 2M → 필요 시 LLTF만 출력, 바이너리, UDP 순으로 검토한다.

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
- [x] RX 펌웨어 `firmware/rx` 작성 (큐+출력 태스크, STATS, csi_recv와 같은 출력 형식). 빌드 확인, 보드 실측 전
- [ ] `firmware/rx` 실측 → 921600 vs 2M 비교, 보레이트 확정
- [ ] RX 출력 형식 확정 — 현재 csi_recv long format 그대로, `rx_id`는 로거 `rx`(포트) 열로 대체 중
- [ ] 움직임 판별 → 호흡 검출 → 상태 머신 → 알림(텔레그램 등, 이후 AWS IoT Core/Lambda/SNS 확장)
- [ ] 보드 2대 추가 도착 후 RX2(출입문) 구성, 다중 RX를 `id` 기준으로 정렬

## 하지 말 것
- `esp-csi\` 원본 수정
- `data\`, `build\` 커밋 (`sdkconfig`는 커밋해서 설정 공유)
- TX/RX 한쪽만 채널·MAC·대역폭 변경
- 실험 영상 보관 (라벨링 후 폐기, 참가자 동의 필수)
