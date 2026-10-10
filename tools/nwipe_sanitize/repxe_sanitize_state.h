/* re-pxe's unattended dispatch for nwipe 0.43; GPL-2.0-or-later. */
#ifndef REPXE_SANITIZE_STATE_H
#define REPXE_SANITIZE_STATE_H

typedef enum { REPXE_SAN_UNKNOWN, REPXE_SAN_IDLE, REPXE_SAN_BUSY, REPXE_SAN_SUCCESS, REPXE_SAN_FAILED, REPXE_SAN_FROZEN } repxe_san_state;
typedef struct { repxe_san_state state; int progress; int action_matches; int eta; } repxe_san_sample;
typedef struct {
    void *opaque;
    int (*poll)(void *, repxe_san_sample *);
    int (*start)(void *);
    int (*interrupted)(void *);
    int (*observe)(void *, const char *, const repxe_san_sample *, int);
    void (*wait)(void *);
} repxe_san_backend;
/* 20 = safe pre-start refusal; 21 = confirmed failure; 22 = outcome unknown.
 * A failed start is uncertain because transport failure can follow acceptance.
 * Only a known supported idle state may cause a single destructive dispatch. */
int repxe_san_run( repxe_san_backend *b );
#endif
