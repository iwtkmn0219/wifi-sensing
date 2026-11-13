# TX — CSI 송신기

> 담당: 민준 · 기반: esp-csi `examples/get-started/csi_send` (esp-csi `8633d67` 복사 + 송신 통계 로그 추가 + 송신 주기 `vTaskDelayUntil` 방식)
> 보드: ESP32 rev v3.1, 플래시 4MB, CH340 USB-시리얼 (부팅 로그로 확인)

일정한 주기로 ESP-NOW 패킷을 브로드캐스트해서 RX가 CSI를 측정할 수 있게 합니다.
TX는 CSI를 계산하지 않습니다. **같은 채널, 같은 MAC, 일정한 주기**로 보내는 것이 역할의 전부입니다.

```
[TX] ──ESP-NOW 브로드캐스트──▶ [RX] ──시리얼(CSI_DATA)──▶ [PC: pc/csi_logger.py]
```

## TX ↔ RX 계약 (변경 시 양쪽 동시 수정)

| 항목 | 값 |
|---|---|
| 채널 | 11 (`CONFIG_LESS_INTERFERENCE_CHANNEL`) |
| 송신 MAC | `1a:00:00:00:00:00` (`esp_wifi_set_mac`으로 고정, 보드 고유 MAC 아님) |
| 방식 | ESP-NOW 브로드캐스트 `FF:FF:FF:FF:FF:FF`, **50Hz** (`CONFIG_SEND_FREQUENCY`) |
| 속도 | MCS0 LGI, phymode HT40 (`esp_now_set_peer_rate_config`) |
| 페이로드 | `uint32_t count` = 시퀀스 번호 → RX 출력의 `id` 필드 |
| 절전 | `WIFI_PS_NONE` |

채널·MAC·대역폭은 TX만 바꾸면 안 됩니다. RX 펌웨어도 같이 바꿔야 합니다.
송신 주파수는 RX 시리얼 보레이트와 짝입니다: **50Hz ↔ 921600**(현재), 100Hz ↔ 2M. 바꿀 때는 RX 보레이트와 PC 로거 `--baud`도 함께 바꿉니다.

## 빌드 · 플래시

`IDF_v5.5.6_Powershell` 터미널에서 실행합니다. 일반 PowerShell에서는 `idf.py`가 동작하지 않습니다.

```powershell
cd E:\esp\wifi-sensing\firmware\tx
idf.py build
idf.py -p COMx erase-flash         # 새 보드 최초 1회
idf.py -p COMx flash monitor       # 모니터 종료: Ctrl+]
```

- `sdkconfig`가 커밋되어 있고 타겟이 이미 `esp32`이므로 `set-target`은 하지 않아도 됩니다. 실행하면 `sdkconfig`가 `sdkconfig.defaults` 기준으로 다시 만들어집니다.
- 정상 부팅 시 `csi_send: wifi_channel: 11, send_frequency: 50, mac: 1a:00:00:00:00:00`가 나오고, 이후 5초마다 송신 통계가 나옵니다.
- `send_frequency`는 **설정값 상수**라 실제 송신 주기가 아닙니다. 실제 주기는 `tx_stats`의 `send`/`on-air`를 봅니다.
- 플래시가 됐는지는 RX 쪽에서도 확인할 수 있습니다. TX가 재부팅되면 RX의 `id`가 0부터 다시 시작합니다(같은 MAC을 쓰는 다른 TX가 켜져 있으면 구분되지 않으니 주의).

## 송신 통계 로그 (원본에 추가한 부분)

```
I (5679) csi_send: tx_stats: send 91.0 Hz, on-air 91.0 Hz, send_err 0, cb_fail 0
```
(위는 `usleep` 방식일 때의 실측 예시입니다. 현재 설정에서는 약 50Hz가 나와야 합니다.)

| 필드 | 의미 |
|---|---|
| `send` | `esp_now_send` 호출 빈도 = 송신 루프의 실제 주기 |
| `on-air` | 송신 콜백 성공 빈도. 브로드캐스트는 ACK가 없으므로 "공중으로 나갔다"는 뜻이고, RX 수신 여부는 RX에서 확인합니다 |
| `send_err` | `esp_now_send` 호출 실패 횟수 (버퍼 부족 등) |
| `cb_fail` | 송신 콜백 실패 횟수 |

주기는 `CONFIG_TX_STATS_INTERVAL_MS`(5000)로 바꿀 수 있습니다. TX↔RX 계약과는 관계없는 변경입니다.
- 부팅 시 `Detected size(4096k) larger than ... (2048k)` 경고가 나오는데, 펌웨어가 2MB 기준으로 빌드되어서이며 동작에는 영향이 없습니다.

플래시가 끝나면 TX는 전원만 들어가 있으면 됩니다. USB 충전기에 꽂아 둬도 됩니다.

## 알려진 사항

- **송신 주기 방식 변경** (2026-11-13): 원본은 송신 후 `usleep(10ms)`로 대기해서 실제 주기가 **약 91Hz**였습니다(2026-10-27 실측, 20초간 90.8~91.0Hz, 오류 0). 송신 처리 시간만큼 주기가 늘어난 것으로 추정합니다. 지금은 `vTaskDelayUntil`로 마지막 깨어난 시각 기준 `1000 / CONFIG_SEND_FREQUENCY` tick(`FREERTOS_HZ=1000`)마다 보냅니다. 100Hz 설정에서 RX `id`·보드 시각 기준 **100.00Hz, 간격 10.00ms**를 확인했습니다(2026-11-13, 세션 `20261113_174751`).
  - `CONFIG_SEND_FREQUENCY`를 바꿀 때는 1000의 약수여야 합니다(아니면 빌드 오류).
- **송신 주파수 50Hz** (2026-11-13, 브랜치 `rate-50hz-921600`): RX를 921600으로 낮추기 위해 100Hz → 50Hz. 2M에서는 PC 수신 바이트 손실로 CSI 행 약 11%가 손상되었고, 921600은 50Hz 전체 CSI를 약 71% 부하로 보낼 수 있습니다(계산값). RX 기준 **50.00Hz, 간격 20.00ms**, 시리얼 손상 0을 확인했습니다(2026-11-13, 세션 `20261113_181550`).
- **재부팅**: TX를 재부팅하면 `count`가 0부터 다시 시작합니다. PC 로거에는 `seq 리셋`으로 표시되고, 데이터는 정상입니다.

## 배치

- **침대를 TX와 RX1 사이에** 둡니다.
- TX–RX 거리는 2~5m, 높이는 0.7~1.2m로 합니다.
- 실험 중에는 TX 위치와 방향을 바꾸지 않습니다. 바뀌면 CSI 기준선 전체가 달라집니다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| RX에 패킷이 전혀 안 들어옴 | TX 전원과 부팅 로그, RX와 채널·MAC이 일치하는지 |
| 수신 Hz가 송신 주파수(50Hz)보다 크게 낮음 | RX `STATS`의 `seq_lost`(무선)·`queue_drop`(시리얼), TX–RX 거리, 채널 혼잡(채널 변경은 TX/RX 동시) |
