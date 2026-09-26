/*
 * lanc_tx.c — deterministic LANC transmitter for Raspberry Pi (libgpiod).
 *
 * Protocol per fred-dev/arduino_lanC reference (tested on Canon XF300):
 *  - wait for the inter-frame idle HIGH >=5ms, then the next FALLING edge
 *    = frame sync (camera drives it)
 *  - wait 104us (camera's start bit), then drive ONLY the 8 data bits of
 *    byte0, LSB first (bit 0 = pull line LOW, bit 1 = release HIGH)
 *  - release (input) for the stop bit, wait for the camera's byte-1 start
 *    falling edge, then drive byte1's 8 data bits the same way
 *  - release, go back to waiting for the next frame
 *
 * Electrical: GPIO17 -> 1k -> diode -> LANC ring. GPIO OUTPUT LOW pulls the
 * bus low through the diode; OUTPUT HIGH / INPUT = released (bus floats to
 * camera pull-up). Same as the pigpio wave path it replaces, but with
 * ~20us kernel event latency instead of Python's ~950us callback.
 *
 * Control protocol: reads commands from FIFO /tmp/lanc_tx.fifo, one per
 * line: "<c0hex> <c1hex> <frames>\n". Each command drives `frames`
 * consecutive frames starting at the next detected frame sync.
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

static struct gpiod_line *line;

static void sleep_until(struct timespec *ts)
{
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, ts, NULL);
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

/* wait for an edge; returns 0 on match, 1 on timeout, -1 on error */
static int wait_event(int want, int timeout_us, struct timespec *out)
{
    struct timespec dl, ev;
    now_ts(&dl);
    ts_add_us(&dl, timeout_us);
    for (;;) {
        now_ts(&ev);
        if (ts_us(&ev) >= ts_us(&dl))
            return 1;
        long remain = ts_us(&dl) - ts_us(&ev);
        struct timespec w = {remain / 1000000, (remain % 1000000) * 1000};
        int r = gpiod_line_event_wait(line, &w);
        if (r < 0)
            return -1;
        if (r == 0) /* timed out on the line itself */
            continue;
        struct gpiod_line_event e;
        if (gpiod_line_event_read(line, &e) < 0)
            return -1;
        int got = (e.event_type == GPIOD_LINE_EVENT_FALLING_EDGE) ? 0 : 1;
        now_ts(out); /* close enough (~10-20us) to the event timestamp */
        if (got == want)
            return 0;
    }
}

static void set_input(void)  { gpiod_line_set_direction_input(line); }
static void set_drive(int v) { gpiod_line_set_direction_output(line, v); }

/* wait for the frame sync: HIGH >=5ms then the next falling edge */
static int wait_sync(struct timespec *t0)
{
    struct timespec t;
    for (;;) {
        set_input();
        if (wait_event(1, 250000, &t) != 0) /* rising */
            continue;
        int r = wait_event(0, IDLE_US, &t); /* falling within 5ms? */
        if (r == 0)
            continue; /* short high -> still inside a frame, keep looking */
        if (r < 0)
            return -1;
        /* line stayed HIGH >=5ms: the next falling edge is the sync */
        if (wait_event(0, 250000, t0) != 0)
            return -1;
        return 0;
    }
}

/* drive 8 data bits (LSB first) starting one bit after ts_edge */
static void drive_byte(unsigned byte, struct timespec ts_edge)
{
    for (int i = 0; i < 8; i++) {
        ts_add_us(&ts_edge, 0); /* keep a stable copy below */
        struct timespec dl;
        now_ts(&dl);
        long base = ts_us(&ts_edge);
        dl.tv_sec = base / 1000000;
        dl.tv_nsec = (base % 1000000) * 1000;
        ts_add_us(&dl, BIT_US);
        sleep_until(&dl);
        set_drive((byte >> i) & 1 ? 1 : 0);
        ts_add_us(&ts_edge, BIT_US);
    }
}

static void drive_frame(unsigned c0, unsigned c1)
{
    struct timespec t0, t1, rel;

    if (wait_sync(&t0) != 0)
        return;

    /* byte 0: data bits at t0 + (1..8)*104us */
    rel = t0;
    for (int i = 0; i < 8; i++) {
        ts_add_us(&rel, BIT_US);
        sleep_until(&rel);
        set_drive((c0 >> i) & 1 ? 1 : 0);
    }
    /* stop bit: release (line goes high via camera pull-up) */
    set_input();

    /* byte 1 start: camera pulls low; wait for that falling edge */
    if (wait_event(0, 600, &t1) != 0) {
        set_input();
        return;
    }
    for (int i = 0; i < 8; i++) {
        ts_add_us(&t1, BIT_US);
        sleep_until(&t1);
        set_drive((c1 >> i) & 1 ? 1 : 0);
    }
    set_input();
}

int main(void)
{
    struct gpiod_chip *chip = gpiod_chip_open_by_name("gpiochip0");
    if (!chip) {
        perror("gpiod_chip_open_by_name");
        return 1;
    }
    line = gpiod_chip_get_line(chip, GPIO);
    if (!line) {
        perror("gpiod_chip_get_line");
        return 1;
    }
    set_input();

    FILE *fifo = fopen(FIFO_PATH, "r");
    if (!fifo) {
        perror("open fifo");
        return 1;
    }

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
