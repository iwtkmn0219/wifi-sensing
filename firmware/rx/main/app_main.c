/*
 * firmware/rx — CSI 수신기
 *
 * TX(esp-csi csi_send, 무수정)의 ESP-NOW 브로드캐스트에서 CSI를 추출해
 * 패킷 1개당 1줄씩 시리얼로 출력한다. 출력 형식은 esp-csi csi_recv(ESP32 long format)와 같다.
 *   → pc/csi_logger.py, pc/csi_io.py 를 수정 없이 그대로 쓴다.
 *
 * Based on esp-csi/examples/get-started/csi_recv (Apache-2.0, Espressif).
 * Target: ESP-IDF v5.5.6, esp32
 *
 * csi_recv 대비 바뀐 점 (출력 형식은 동일)
 *   1. CSI 콜백은 복사 + 큐 전송만 한다. 출력은 별도 태스크 (콜백 안 ets_printf 로 인한 수신 누락 방지)
 *   2. 헤더 줄을 부팅 때 1회 + 주기적으로 다시 출력 (로거를 늦게 켜도 필드 매핑 가능)
 *   3. STATS 줄로 손실 원인을 나눠서 보여준다: seq_lost(무선 구간) / queue_drop(시리얼 병목)
 *   4. id 는 payload 가 ESP-NOW 프레임일 때만 꺼낸다 (예제는 검사 없이 payload+15 를 읽음)
 *   ESP32 는 gain control 을 쓰지 않으므로(예제도 동일) esp_csi_gain_ctrl 의존성이 없다.
 */
#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_now.h"
#include "esp_timer.h"

#define RX_CHANNEL          CONFIG_CSI_WIFI_CHANNEL
#if CONFIG_CSI_WIFI_HT40
#define RX_BW               WIFI_BW_HT40
#define RX_SECOND_CHAN      WIFI_SECOND_CHAN_BELOW
#define RX_PHYMODE          WIFI_PHY_MODE_HT40
#define RX_BW_STR           "HT40-"
#else
#define RX_BW               WIFI_BW_HT20
#define RX_SECOND_CHAN      WIFI_SECOND_CHAN_NONE
#define RX_PHYMODE          WIFI_PHY_MODE_HT20
#define RX_BW_STR           "HT20"
#endif
#if CONFIG_CSI_OUTPUT_LLTF_ONLY
#define RX_LLTF_ONLY        1
#else
#define RX_LLTF_ONLY        0
#endif

#define CSI_BUF_MAX         640     /* esp32: LLTF 128 + HT-LTF 256 + STBC 256 이하 */
#define ESPNOW_BODY_OFFSET  15      /* payload(802.11 헤더 뒤) 기준 ESP-NOW body 위치. csi_recv 와 동일 */
#define ESPNOW_CATEGORY     127     /* vendor-specific action frame */

/* csi_recv(ESP32) 헤더와 글자 하나까지 동일해야 한다 — pc/csi_logger.py LONG_FMT */
#define CSV_HEADER "type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation," \
                   "stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp," \
                   "ant,sig_len,rx_format,len,first_word,data\n"

static const char *TAG = "csi_rx";
static const uint8_t TX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

typedef struct {
    wifi_pkt_rx_ctrl_t rx_ctrl;
    uint8_t  mac[6];
    uint8_t  first_word;
    uint32_t id;             /* TX count */
    uint16_t len;            /* 출력할 CSI 바이트 수 */
    int8_t   buf[CSI_BUF_MAX];
} csi_pkt_t;

static QueueHandle_t s_csi_queue;

/* 콜백(Wi-Fi 태스크)에서만 쓰는 값 */
static csi_pkt_t s_scratch;
static uint32_t  s_last_id;
static bool      s_have_id = false;

/* 통계: 콜백에서 증가, 출력 태스크에서 읽기 */
static volatile uint32_t s_rx_cnt;        /* TX MAC 필터 통과 */
static volatile uint32_t s_queue_drop;    /* 큐 가득 참 = 출력(시리얼) 병목 */
static volatile uint32_t s_seq_lost;      /* id 빈 구간 = 무선 구간 손실 */
static volatile uint32_t s_bad_payload;   /* ESP-NOW payload 아님 → 출력 안 함 */
static volatile int32_t  s_rssi_sum;

/* ---------------- Wi-Fi / ESP-NOW (csi_recv 와 동일 설정) ---------------- */
static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, RX_BW));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(RX_CHANNEL, RX_SECOND_CHAN));
    /* csi_recv 는 RX 자신의 MAC 도 1a:00:00:00:00:00 으로 바꾸지만, 수신에는 필요 없어서
     * 보드 고유 MAC 을 유지한다 (여러 RX 구분용으로 부팅 로그에 출력). */
}

static void esp_now_setup(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *) "pmk1234567890123"));

    esp_now_peer_info_t peer = {
        .channel   = RX_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    esp_now_rate_config_t rate_config = {
        .phymode = RX_PHYMODE,
        .rate    = WIFI_PHY_RATE_MCS0_LGI,
        .ersu    = false,
        .dcm     = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

/* ---------------- CSI 콜백: 필터 → 복사 → 큐 (출력 없음) ---------------- */
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf || memcmp(info->mac, TX_MAC, 6) != 0) {
        return;
    }
    if (!info->payload || info->payload_len < ESPNOW_BODY_OFFSET + 4 || info->payload[0] != ESPNOW_CATEGORY) {
        s_bad_payload++;
        return;
    }

    csi_pkt_t *p = &s_scratch;
    p->rx_ctrl    = info->rx_ctrl;
    memcpy(p->mac, info->mac, 6);
    p->first_word = info->first_word_invalid;
    memcpy(&p->id, info->payload + ESPNOW_BODY_OFFSET, sizeof(uint32_t));

    uint16_t len = info->len;
#if RX_LLTF_ONLY
    if (len > 128) len = 128;
#endif
    if (len > CSI_BUF_MAX) len = CSI_BUF_MAX;
    p->len = len;
    memcpy(p->buf, info->buf, len);

    s_rx_cnt++;
    s_rssi_sum += info->rx_ctrl.rssi;
    if (s_have_id && p->id > s_last_id + 1) {
        s_seq_lost += p->id - s_last_id - 1;   /* id 감소(TX 재부팅)는 손실로 세지 않음 */
    }
    s_last_id = p->id;
    s_have_id = true;

    if (xQueueSend(s_csi_queue, p, 0) != pdTRUE) {
        s_queue_drop++;
    }
}

static void wifi_csi_init(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /* csi_recv(esp32) 기본값 그대로. 바꿀 때는 firmware/rx/README.md 에 이유를 기록 */
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

/* ---------------- 출력 태스크 ---------------- */
static inline char *put_int(char *o, int32_t v)
{
    char tmp[12];
    int n = 0;
    uint32_t u = (v < 0) ? (uint32_t)(-(int64_t) v) : (uint32_t) v;
    if (v < 0) *o++ = '-';
    do { tmp[n++] = (char)('0' + u % 10); u /= 10; } while (u);
    while (n) *o++ = tmp[--n];
    return o;
}

static void print_task(void *arg)
{
    static csi_pkt_t p;
    static char line[256 + CSI_BUF_MAX * 5];

    const int64_t stats_us = (int64_t) CONFIG_CSI_STATS_INTERVAL_S * 1000000;
    int64_t t_last = esp_timer_get_time();
    uint32_t last_rx = 0, last_drop = 0, last_lost = 0, last_bad = 0;
    int32_t last_rssi_sum = 0;

    fputs(CSV_HEADER, stdout);

    for (;;) {
        if (xQueueReceive(s_csi_queue, &p, pdMS_TO_TICKS(200)) == pdTRUE) {
            const wifi_pkt_rx_ctrl_t *rx = &p.rx_ctrl;
            /* csi_recv 의 ets_printf 와 같은 필드·순서. rx_format 자리에 sig_mode 를 쓰는 것도 예제와 동일 */
            int n = snprintf(line, 256,
                             "CSI_DATA,%" PRIu32 "," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%" PRIu32 ",%d,%d,%d,%d,%d,\"[",
                             p.id, MAC2STR(p.mac), rx->rssi, rx->rate, rx->sig_mode,
                             rx->mcs, rx->cwb, rx->smoothing, rx->not_sounding,
                             rx->aggregation, rx->stbc, rx->fec_coding, rx->sgi,
                             rx->noise_floor, rx->ampdu_cnt, rx->channel, rx->secondary_channel,
                             (uint32_t) rx->timestamp, rx->ant, rx->sig_len, rx->sig_mode,
                             p.len, p.first_word);
            char *o = line + n;
            for (int i = 0; i < p.len; i++) {
                if (i) *o++ = ',';
                o = put_int(o, p.buf[i]);
            }
            *o++ = ']'; *o++ = '"'; *o++ = '\n';
            fwrite(line, 1, o - line, stdout);
        }

        int64_t now = esp_timer_get_time();
        if (now - t_last >= stats_us) {
            uint32_t rx = s_rx_cnt, drop = s_queue_drop, lost = s_seq_lost, bad = s_bad_payload;
            int32_t rssi_sum = s_rssi_sum;
            uint32_t d_rx = rx - last_rx, d_lost = lost - last_lost;
            float secs = (now - t_last) / 1e6f;
            /* 로거는 CSI_DATA / type, 로 시작하지 않는 줄을 무시한다 */
            printf("STATS,uptime_s=%" PRId64 ",rx_hz=%.1f,seq_lost=%" PRIu32 ",loss_pct=%.2f,queue_drop=%" PRIu32
                   ",bad_payload=%" PRIu32 ",rssi_avg=%.1f,free_heap=%" PRIu32 "\n",
                   now / 1000000, d_rx / secs, d_lost,
                   (d_rx + d_lost) ? 100.0f * d_lost / (d_rx + d_lost) : 0.0f,
                   drop - last_drop, bad - last_bad,
                   d_rx ? (float)(rssi_sum - last_rssi_sum) / d_rx : 0.0f,
                   esp_get_free_heap_size());
            fputs(CSV_HEADER, stdout);   /* 로거를 늦게 켜도 헤더를 받도록 */
            last_rx = rx; last_drop = drop; last_lost = lost; last_bad = bad; last_rssi_sum = rssi_sum;
            t_last = now;
        }
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    s_csi_queue = xQueueCreate(CONFIG_CSI_QUEUE_LEN, sizeof(csi_pkt_t));
    if (!s_csi_queue) {
        ESP_LOGE(TAG, "queue alloc failed");
        return;
    }

    wifi_init();
    esp_now_setup();

    uint8_t own_mac[6];
    ESP_ERROR_CHECK(esp_read_mac(own_mac, ESP_MAC_WIFI_STA));
    ESP_LOGI(TAG, "================ CSI RX ================");
    ESP_LOGI(TAG, "own_mac=" MACSTR "  filter_tx_mac=" MACSTR, MAC2STR(own_mac), MAC2STR(TX_MAC));
    ESP_LOGI(TAG, "channel=%d  bw=%s  lltf_only=%d  baud=%d",
             RX_CHANNEL, RX_BW_STR, RX_LLTF_ONLY, CONFIG_ESP_CONSOLE_UART_BAUDRATE);

    xTaskCreatePinnedToCore(print_task, "csi_print", 4096, NULL, 5, NULL, 1);
    wifi_csi_init();
}
