#ifndef ORDER_LAYER_H
#define ORDER_LAYER_H

#include <stdint.h>

/* Shared-memory layout for Conveyance: Order Layer (C) writes, Commander
 * (Python) reads. One writer, any number of readers, no locks — readers
 * detect a torn entry via the seqlock-style pulse counter (odd = writer
 * mid-update, even = stable) instead of blocking the writer. */

#define ORDER_LAYER_SHM_NAME "/order_layer"
#define RING_SLOTS      4096          /* power of 2 */
#define PAYLOAD_SIZE    32

#define REFLEX_CALM     0
#define REFLEX_ALERT    1
#define REFLEX_WITHDRAW 2
#define REFLEX_PARK     3
#define REFLEX_RECOVER  4

/* Substrate-level signal state: does the Order Layer have a live admitted
 * measurement at all, independent of what reflex concluded from it. This
 * is what lets Commander tell "reflex=WITHDRAW because drift is genuinely
 * bad" apart from "reflex=WITHDRAW because there's no signal to judge" —
 * today reflex_state alone conflates the two. */
#define SIGNAL_LIVE  0   /* fresh measurement within STALE_WINDOW_S */
#define SIGNAL_STALE 1   /* no fresh measurement, but within DEAD_WINDOW_S */
#define SIGNAL_DEAD  2   /* no measurement for longer than DEAD_WINDOW_S — peer presumed gone */

typedef struct __attribute__((packed)) {
    uint64_t tick_id;
    uint8_t  payload[PAYLOAD_SIZE];
    float    drift;         /* pd_dev-equivalent at time of admission */
    uint8_t  reflex_state;
    uint8_t  signal_state;  /* SIGNAL_LIVE/STALE/DEAD at time of admission */
    uint8_t  admissible;    /* 1 = passed forward-cone check, always 1 for entries actually written */
    uint64_t t_ns;          /* CLOCK_MONOTONIC_RAW at admission */
} RingEntry;

/* Not packed: pulse/write_idx must sit on their natural 8-byte boundary
 * for the seqlock read/write to actually be atomic on the bus. */
typedef struct {
    uint32_t magic;         /* 0x4F524C31 "ORL1" */
    volatile uint64_t pulse;   /* seqlock: odd = write in progress, even = stable. Commander polls this to pace itself. */
    uint64_t write_idx;     /* monotonically increasing; slot = write_idx % RING_SLOTS */
    uint8_t  reflex_state;
    uint8_t  signal_state;
    float    drift;
    RingEntry ring[RING_SLOTS];
} OrderLayerShm;

#define ORDER_LAYER_MAGIC 0x4F524C31u

#endif
