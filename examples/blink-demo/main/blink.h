#pragma once
#include <stdint.h>

/* 把 period_ms 换算成半周期（亮 / 灭各占一半） */
uint32_t blink_half_period_ms(uint32_t period_ms);
void blink_init(int gpio);
void blink_set(int gpio, int on);
