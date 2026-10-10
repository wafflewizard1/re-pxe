#include "repxe_sanitize_state.h"

int repxe_san_run( repxe_san_backend *b )
{
    repxe_san_sample sample = { REPXE_SAN_UNKNOWN, 0, 0, -1 };
    if( b->poll( b->opaque, &sample ) )
    {
        b->observe( b->opaque, "preflight_unknown", &sample, 0 );
        return 22;
    }
    if( sample.state == REPXE_SAN_BUSY || sample.state == REPXE_SAN_UNKNOWN )
    {
        b->observe( b->opaque, "preexisting_or_unknown", &sample, 0 );
        return 22;
    }
    if( sample.state != REPXE_SAN_IDLE && sample.state != REPXE_SAN_SUCCESS )
    {
        b->observe( b->opaque, "preflight_refused", &sample, 0 );
        return 22;
    }
    if( b->interrupted( b->opaque ) ) return 22;
    /* Journal the intent durably BEFORE dispatch; failure must not issue a command. */
    if( b->observe( b->opaque, "starting", &sample, 0 ) ) return 22;
    if( b->start( b->opaque ) )
    {
        sample.state = REPXE_SAN_UNKNOWN;
        b->observe( b->opaque, "start_unknown", &sample, 1 );
        return 22;
    }
    sample.state = REPXE_SAN_BUSY;
    sample.progress = 0;
    if( b->observe( b->opaque, "accepted", &sample, 1 ) ) return 22;
    for( ;; )
    {
        if( b->interrupted( b->opaque ) )
        {
            b->observe( b->opaque, "monitoring_interrupted", &sample, 1 );
            return 22;
        }
        b->wait( b->opaque );
        if( b->poll( b->opaque, &sample ) )
        {
            sample.state = REPXE_SAN_UNKNOWN;
            b->observe( b->opaque, "status_unknown", &sample, 1 );
            return 22;
        }
        if( !sample.action_matches || ( sample.state != REPXE_SAN_BUSY
           && sample.state != REPXE_SAN_SUCCESS && sample.state != REPXE_SAN_FAILED ) )
        {
            b->observe( b->opaque, "status_unknown", &sample, 1 );
            return 22;
        }
        if( b->observe( b->opaque, "progress", &sample, 1 ) ) return 22;
        if( sample.state == REPXE_SAN_SUCCESS ) return 0;
        if( sample.state == REPXE_SAN_FAILED ) return 21;
    }
}
