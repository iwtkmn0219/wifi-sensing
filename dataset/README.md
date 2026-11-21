# dataset — 공유용 CSI 측정 데이터

`data\`(로거 저장 위치, git 제외) 중 **분석·발표에 쓸 세션만** 골라 압축해서 커밋한 폴더입니다.
새 세션을 추가할 때는 아래 "추가 방법"을 따르고 이 목록을 갱신합니다.

- 공개 저장소에 올라가는 데이터입니다. 참가자 동의를 받은 세션만 넣고, 이름·영상·사진 등 개인 식별 정보는 넣지 않습니다.
- CSV는 gzip으로 압축했습니다(원본의 약 20~25%). GitHub 파일 한도(100MB)를 넘지 않도록 **압축 파일 하나가 50MB를 넘으면 세션을 나누거나 LFS를 검토**합니다.

## 공통 조건

| 항목 | 값 |
|---|---|
| TX | `firmware/tx`, ESP-NOW 브로드캐스트 **50Hz**, 채널 11, HT40−, MCS0 |
| RX | `firmware/rx`, 시리얼 **921600bps**, CSI 384B(192 서브캐리어: LLTF 64 + HT-LTF 128) |
| RX 보드 | **COM4 = `b4:bf:e9:fa:e3:08`**, **COM5 = `30:76:f5:92:c2:64`** (ESP32-D0WD-V3 rev v3.1). 11/13 세션의 COM4는 지금의 COM5 보드(`…c2:64`) |
| 시각 정렬 | 두 RX는 같은 TX 패킷을 받으므로 **`id`로 정렬**. `pc_time`은 PC 수신 시각(유닉스 초) |

## 세션

| 폴더 | 날짜·시간 | 내용 | RX | 길이 | 라벨 | 무선 손실(STATS) | RSSI |
|---|---|---|---|---|---|---|---|
| `20261113_181550_50hz_baseline` | 11/13 18:15 | 50Hz + 921600 확정 측정 (사람 움직임 통제 안 함) | COM4(`…c2:64`) | 2분 | `rate50_921600` | 3.65% | −49.8 dBm |
| `20261121_163812_breath` | 11/21 16:38 | 누워서 자연 호흡 + **휴대폰 배 위 가속도**(phyphox) | COM4, COM5 | 11분 | `breath` | 5.72% / 2.68% | −82.0 / −67.1 dBm |
| `20261121_170403_hold` | 11/21 17:04 | 누워서 **숨 참기 7회**(25~37초)와 자연 호흡 반복 | COM4, COM5 | 5.7분 | `hold`, `lying-free` | 2.48% / 3.85% | −80.5 / −68.7 dBm |

세 세션 모두 `queue_drop` 0, 저장된 행의 시리얼 손상 0입니다(`rx_stats.py` 기준).

### 주의할 점
- **11/21 세션의 COM4는 신호가 약합니다**(RSSI −80~−82 dBm). 노이즈 제거 후에도 호흡 신호가 약하게만 보입니다(`pc/csi_io.py` 설명 참고).
- **휴대폰 데이터는 "가속도 (g 제외)"** 라서 호흡 신호가 약합니다. 60초 창마다 추정 호흡수가 분당 9~30회로 흔들려 정답값으로 쓰기 어렵습니다. 다음 측정부터는 "가속도 (g 포함)" 또는 기울기를 권장합니다.
- 숨 참기 세션의 라벨은 측정자가 손으로 입력해서 실제 동작과 약 1초 차이가 날 수 있습니다.

## 파일 구성

```
<세션>/
 ├─ COM4_csi.csv.gz, COM5_csi.csv.gz ← CSI. 패킷 1개 = 1행 (pc_time, label, rx, id, rssi, ..., len, first_word, data)
 ├─ COM4_stats.csv, COM5_stats.csv ← RX 보드 STATS (5초마다, seq_lost·queue_drop 등. 원문은 raw 열)
 ├─ events.csv                     ← 라벨이 바뀐 시각
 └─ phone_linear_accel/            ← (breath 세션만) 휴대폰 phyphox 내보내기
     ├─ raw_data.csv.gz            ← Time (s), Linear Acceleration x/y/z, Absolute (m/s^2), 약 99.5Hz
     └─ meta/time.csv              ← START 행의 system time = Time 0의 유닉스 시각 (CSI pc_time과 같은 기준)
```

`data` 열은 서브캐리어마다 `[허수, 실수]` 순서의 정수 쌍이고, `first_word == 1`이면 앞 서브캐리어 2개가 무효입니다.

## 읽는 법

```powershell
cd E:\esp\wifi-sensing\pc
python rx_stats.py ..\dataset\20261121_170403_hold\*              # 수신 품질 요약 (.csv.gz 그대로 읽음)
python csi_io.py ..\dataset\20261121_170403_hold\COM5_csi.csv.gz --breath 12 --edge 3   # 라벨별 호흡 주기성 점수
```

```python
from csi_io import load_csi, preprocess, breathing_scores
df, amp = load_csi("../dataset/20261121_163812_breath/COM5_csi.csv.gz")   # pandas가 gzip을 자동으로 풂
grid, A = preprocess(df, amp)
```

## 추가 방법

1. `data\<세션>\`에서 쓸 세션을 고르고, 폴더 이름을 `<세션>_<내용>`으로 정합니다.
2. CSI CSV는 `gzip -9`로 압축하고 이름을 `COM4.csv` → `COM4_csi.csv.gz`로 바꿉니다(**`COM4.`으로 시작하는 이름은 Windows 예약 장치 이름이라 git이 읽지 못함**), `*_stats.csv`·`events.csv`는 그대로 복사합니다.
3. 휴대폰 등 보조 데이터는 하위 폴더에 넣고, 파일 이름에 한글·공백을 쓰지 않습니다.
4. 위 "세션" 표에 한 줄 추가합니다(무선 손실·RSSI는 `rx_stats.py` 결과).
