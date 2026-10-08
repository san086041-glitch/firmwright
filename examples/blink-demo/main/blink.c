#include "blink.h"
#include "driver/gpio.h"

uint32_t blink_half_period_ms(uint32_t period_ms)
{
    return period_ms / 2;
}

void blink_init(int gpio)
{
    gpio_reset_pin(gpio);
    gpio_set_direction(gpio, GPIO_MODE_OUTPUT);
}

void blink_set(int gpio, int on)
{
    gpio_set_level(gpio, on);
}
