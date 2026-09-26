/*
 * lanc_tx.c — deterministic LANC transmitter for Raspberry Pi (libgpiod v2).
 *
 * Protocol per fred-dev/arduino_lanC reference (tested on Canon XF300):
 *  - wait for the inter-frame idle HIGH >=5ms, then the next FALLING edge
 *    = frame sync (camera drives it)
 *  - wait 104us (camera's start bit), then drive ONLY the 8 data bits of
 *    byte0, LSB first (bit 0 = pull line LOW, bit 1 = release)
 *  - release for the stop bit, wait for the camera's byte-1 start falling
 *    edge, then drive byte1's 8 data bits the same way
 *  - release, go back to waiting for the next frame
 *
 * Electrical: GPIO17 -> 1k -> diode -> LANC ring. GPIO low pulls the bus
 * low through the diode; INPUT/HIGH = released. We switch the line between
 * INPUT (edge-waiting) and OUTPUT-OPEN-DRAIN (driving) per frame; within a
 * byte, bit "1" = INACTIVE (open-drain released), bit "0" = ACTIVE (low).
 *
 * Control: reads "<c0hex> <c1hex> <frames>\n" from /tmp/lanc_tx.fifo.
 */
#include <gpiod.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <unistd.h>

#define GPIO 17
#define BIT_US 104
#define IDLE_US 5000
#define FIFO_PATH "/tmp/lanc_tx.fifo"

static struct gpiod_chip *chip;
static struct gpiod_line_request *req;
static struct gpiod_line_config *cfg_in;   /* INPUT, edge BOTH */
static struct gpiod_line_config *cfg_out;  /* OUTPUT open-drain, inactive */
static struct gpiod_edge_event_buffer *ebuf;

static void die(const char *m) { perror(m); exit(1); }

static struct gpiod_line_config *make_cfg(int input)
{
    struct gpiod_line_settings *s = gpiod_line_settings_new();
    if (!s)
        die("settings_new");
    unsigned int offset = GPIO;
    struct gpiod_line_config *lc = gpiod_line_config_new();
    if (!lc)
        die("lc_new");
    if (input) {
        gpiod_line_settings_set_direction(s, GPIOD_LINE_DIRECTION_INPUT);
        gpiod_line_settings_set_edge_detection(s, GPIOD_LINE_EDGE_BOTH);
        gpiod_line_settings_set_event_clock(s, GPIOD_LINE_EVENT_CLOCK_MONOTONIC);
    } else {
        gpiod_line_settings_set_direction(s, GPIOD_LINE_DIRECTION_OUTPUT);
        gpiod_line_settings_set_drive(s, GPIOD_LINE_DRIVE_OPEN_DRAIN);
        gpiod_line_settings_set_output_value(s, GPIOD_LINE_VALUE_INACTIVE);
    }
    if (gpiod_line_config_add_line_settings(lc, &offset, 1, s) < 0)
        die("add_line_settings");
    gpiod_line_settings_free(s);
    return lc;
}

static void now_ts(struct timespec *ts) { clock_gettime(CLOCK_MONOTONIC, ts); }

static long ts_us(struct timespec *ts)
{
    return (long)ts->tv_sec * 1000000L + ts->tv_nsec / 1000;
}

static void ts_add_us(struct timespec *ts, long us)
{
    ts->tv_sec += us / 1000000;
    ts->tv_nsec += (us % 1000000) * 1000;
    if (ts->tv_nsec >= 1000000000) {
        ts->tv_sec += 1;
        ts->tv_nsec -= 1000000000;
    }
}

static void sleep_until(struct timespec *ts)
{
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, ts, NULL);
}

static void set_input(void)
{
    if (gpiod_line_request_reconfigure_lines(req, cfg_in) < 0)
        die("reconfigure input");
}

static void set_output(void)
{
    if (gpiod_line_request_reconfigure_lines(req, cfg_out) < 0)
        die("reconfigure output");
}

static void set_drive_bit(int v)
{
    gpiod_line_request_set_value(req, GPIO,
        v ? GPIOD_LINE_VALUE_INACTIVE : GPIOD_LINE_VALUE_ACTIVE);
}

/* wait for an edge; returns 0 on match, 1 on timeout, -1 on error */
static int wait_event(int want, long timeout_us, struct timespec *out)
{
    struct timespec dl, now;
    now_ts(&dl);
    ts_add_us(&dl, timeout_us);
    for (;;) {
        now_ts(&now);
        long remain = ts_us(&dl) - ts_us(&now);
        if (remain <= 0)
            return 1;
        int r = gpiod_line_request_wait_edge_events(req, remain * 1000);
        if (r < 0)
            return -1;
        if (r == 0)
            return 1; /* kernel wait timed out */
        if (gpiod_line_request_read_edge_events(req, ebuf, 1) < 1)
            return -1;
        struct gpiod_edge_event *ev = gpiod_edge_event_buffer_get_event(ebuf, 0);
        if (!ev)
            return -1;
        int got = gpiod_edge_event_get_event_type(ev) ==
                  GPIOD_EDGE_EVENT_FALLING_EDGE ? 0 : 1;
        /* use the kernel timestamp of the edge (ns) instead of sampling
         * clock_gettime after the read (saves ~20us staleness) */
        long long ns = gpiod_edge_event_get_timestamp_ns(ev);
        out->tv_sec = ns / 1000000000LL;
        out->tv_nsec = ns % 1000000000LL;
        if (got == want)
            return 0;
    }
}

/* wait for the frame sync: HIGH >=5ms then the next falling edge */
static int wait_sync(struct timespec *t0)
{
    struct timespec t;
    for (;;) {
        set_input();
        if (wait_event(1, 250000, &t) != 0) /* rising */
            return -1;
        int r = wait_event(0, IDLE_US, &t); /* falling within 5ms? */
        if (r == 0)
            continue; /* short high: still inside a frame */
        if (r < 0)
            return -1;
        if (wait_event(0, 250000, t0) != 0)
            return -1;
        return 0;
    }
}

static void drive_frame(unsigned c0, unsigned c1)
{
    struct timespec t0, t1;

    if (wait_sync(&t0) != 0)
        return;

    /* byte 0: data bits land at t0 + (1..8)*104us */
    set_output();
    struct timespec rel = t0;
    for (int i = 0; i < 8; i++) {
        ts_add_us(&rel, BIT_US);
        sleep_until(&rel);
        set_drive_bit((c0 >> i) & 1);
    }
    set_input(); /* stop bit: line released, floats high */

    /* byte 1 start: camera pulls low; wait for that falling edge */
    if (wait_event(0, 600, &t1) != 0)
        return;
    set_output();
    for (int i = 0; i < 8; i++) {
        ts_add_us(&t1, BIT_US);
        sleep_until(&t1);
        set_drive_bit((c1 >> i) & 1);
    }
    set_input();
}

int main(void)
{
    chip = gpiod_chip_open("/dev/gpiochip0");
    if (!chip)
        die("chip_open");
    req = gpiod_chip_request_lines(chip, NULL, make_cfg(1));
    if (!req)
        die("request_lines");
    cfg_in = make_cfg(1);
    cfg_out = make_cfg(0);
    ebuf = gpiod_edge_event_buffer_new(8);
    if (!ebuf)
        die("ebuf_new");

    FILE *fifo = fopen(FIFO_PATH, "r");
    if (!fifo)
        die("open fifo");

    char buf[128];
    while (fgets(buf, sizeof buf, fifo)) {
        unsigned c0 = 0, c1 = 0;
        int frames = 1;
        if (sscanf(buf, "%x %x %d", &c0, &c1, &frames) < 2)
            continue;
        if (frames < 1)
            frames = 1;
        for (int k = 0; k < frames; k++)
            drive_frame(c0 & 0xFF, c1 & 0xFF);
    }
    return 0;
}
