# TX — CSI 송신기

> 담당: 민준 · 기반: esp-csi `examples/get-started/csi_send` (**수정 없이** 사용, 동작 확인 완료)

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
| 방식 | ESP-NOW 브로드캐스트 `FF:FF:FF:FF:FF:FF`, 100Hz |
| 속도 | MCS0 LGI, phymode HT40 (`esp_now_set_peer_rate_config`) |
| 페이로드 | `uint32_t count` = 시퀀스 번호 → RX 출력의 `id` 필드 |
| 절전 | `WIFI_PS_NONE` |

채널·MAC·대역폭은 TX만 바꾸면 안 됩니다. RX 펌웨어도 같이 바꿔야 합니다.

## 빌드 · 플래시

`IDF_v5.5.6_Powershell` 터미널에서 실행합니다. 일반 PowerShell에서는 `idf.py`가 동작하지 않습니다.

```powershell
cd C:\esp\wifi-sensing\firmware\tx
idf.py set-target esp32            # ⚠️ 보드 모델(ESP32/S3) 최종 확인 전
idf.py build
idf.py -p COMx erase-flash         # 새 보드 최초 1회
idf.py -p COMx flash monitor       # 모니터 종료: Ctrl+]
```

플래시가 끝나면 TX는 전원만 들어가 있으면 됩니다. USB 충전기에 꽂아 둬도 됩니다.

## 알려진 사항

- **실제 주기**: 송신 후 `usleep`으로 대기하는 방식이라 실제 주기가 100Hz보다 약간 낮을 수 있습니다(미측정). RX 측정 결과가 나쁘면 `vTaskDelayUntil` 방식으로 바꿉니다. 주기만 바꾸는 것이므로 RX와 PC는 수정하지 않아도 됩니다.
- **재부팅**: TX를 재부팅하면 `count`가 0부터 다시 시작합니다. PC 로거에는 `seq 리셋`으로 표시되고, 데이터는 정상입니다.

## 배치

- **침대를 TX와 RX1 사이에** 둡니다.
- TX–RX 거리는 2~5m, 높이는 0.7~1.2m로 합니다.
- 실험 중에는 TX 위치와 방향을 바꾸지 않습니다. 바뀌면 CSI 기준선 전체가 달라집니다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| RX에 패킷이 전혀 안 들어옴 | TX 전원과 부팅 로그, RX와 채널·MAC이 일치하는지 |
| 수신 Hz가 100보다 크게 낮음 | 위 "실제 주기" 항목, TX–RX 거리, 채널 혼잡(채널 변경은 TX/RX 동시) |
