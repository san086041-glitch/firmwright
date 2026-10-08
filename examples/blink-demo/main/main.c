#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "blink.h"

#define BLINK_GPIO 2
#define BLINK_PERIOD_MS 1000

static const char *TAG = "blink";

void app_main(void)
{
    ESP_LOGI(TAG, "app_main started");
    blink_init(BLINK_GPIO);

    uint32_t half = blink_half_period_ms(BLINK_PERIOD_MS);
    printf("TEST:half_period:%s\n", half == 500 ? "PASS" : "FAIL");

    int on = 0;
    for (int i = 0;; i++) {
        on = !on;
        blink_set(BLINK_GPIO, on);
        if (i < 6) {
            ESP_LOGI(TAG, "led %s", on ? "on" : "off");
        }
        vTaskDelay(pdMS_TO_TICKS(half));
    }
}
