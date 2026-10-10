#include "repxe_sanitize_state.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    repxe_san_sample samples[8]; int count,index,starts,start_error,poll_error,interrupt_after,event_error,events;
} mock;
static int poll(void *p,repxe_san_sample *s) {
    mock *m=p;
    if(m->poll_error && m->index==m->poll_error-1) return -1;
    assert(m->index<m->count); *s=m->samples[m->index++]; return 0;
}
static int start(void *p) { mock *m=p; m->starts++; assert(m->starts==1); return m->start_error; }
static int interrupted(void *p) { mock *m=p; return m->interrupt_after && m->index>=m->interrupt_after; }
static int observe(void *p,const char *kind,const repxe_san_sample *s,int attempted) {
    mock *m=p; (void)s; m->events++;
    if(!strcmp(kind,"starting")) assert(!attempted && m->starts==0);
    if(!strcmp(kind,"accepted") || !strcmp(kind,"progress")) assert(attempted && m->starts==1);
    return m->event_error && m->events==m->event_error;
}
static void wait_(void *p) { (void)p; }
static int run(mock *m) { repxe_san_backend b={m,poll,start,interrupted,observe,wait_}; return repxe_san_run(&b); }
static repxe_san_sample sample(repxe_san_state s,int progress,int matches) { repxe_san_sample a={s,progress,matches,-1}; return a; }
int main(void) {
    int checks=0; mock m={0};
    m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.samples[1]=sample(REPXE_SAN_BUSY,37,1); m.samples[2]=sample(REPXE_SAN_SUCCESS,100,1); m.count=3;
    assert(run(&m)==0 && m.starts==1 && m.index==3); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_SUCCESS,100,0); m.samples[1]=sample(REPXE_SAN_SUCCESS,100,1); m.count=2;
    assert(run(&m)==0 && m.starts==1 && m.index==2); checks++; /* Historical success alone never finishes a new run. */
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_BUSY,20,1); m.count=1;
    assert(run(&m)==22 && !m.starts); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_UNKNOWN,0,1); m.count=1;
    assert(run(&m)==22 && !m.starts); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_FROZEN,0,1); m.count=1;
    assert(run(&m)==22 && !m.starts); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_FAILED,0,1); m.count=1;
    assert(run(&m)==22 && !m.starts); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.count=1; m.start_error=-1;
    assert(run(&m)==22 && m.starts==1); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.samples[1]=sample(REPXE_SAN_FAILED,0,1); m.count=2;
    assert(run(&m)==21 && m.starts==1); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.samples[1]=sample(REPXE_SAN_UNKNOWN,0,1); m.count=2;
    assert(run(&m)==22 && m.starts==1); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.samples[1]=sample(REPXE_SAN_SUCCESS,100,0); m.count=2;
    assert(run(&m)==22 && m.starts==1); checks++; /* Mismatched/stale NVMe action cannot certify success. */
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.samples[1]=sample(REPXE_SAN_BUSY,45,1); m.count=2; m.interrupt_after=2;
    assert(run(&m)==22 && m.starts==1); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.count=1; m.poll_error=2;
    assert(run(&m)==22 && m.starts==1); checks++;
    memset(&m,0,sizeof(m)); m.count=1; m.poll_error=1;
    assert(run(&m)==22 && !m.starts); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.count=1; m.event_error=1;
    assert(run(&m)==22 && !m.starts); checks++;
    memset(&m,0,sizeof(m)); m.samples[0]=sample(REPXE_SAN_IDLE,0,1); m.count=1; m.event_error=2;
    assert(run(&m)==22 && m.starts==1); checks++;
    printf("PASS: %d mocked sanitize lifecycle cases; no physical devices opened\n",checks);
}
