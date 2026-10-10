/* Explicit unattended sanitize dispatch inside nwipe; GPL-2.0-or-later.
 * Upstream se_ata/se_nvme perform every firmware command and status query.
 * Only an exact idle target may be dispatched, once. Unknown is never success.
 */
#ifdef HAVE_CONFIG_H
#include <config.h>
#endif
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "nwipe.h"
#include "context.h"
#include "options.h"
#include "logging.h"
#include "device.h"
#include "gui.h"
#include "create_pdf.h"
#include "miscellaneous.h"
#include "se_ata.h"
#include "se_nvme.h"
#include "repxe_sanitize.h"
#include "repxe_sanitize_state.h"
#include "repxe_binding.h"

extern WINDOW *main_window, *footer_window, *options_window, *stats_window;
extern nwipe_thread_data_ptr_t *global_nwipe_thread_data_ptr;
extern int terminate_signal;

typedef struct {
    nwipe_context_t *ctx;
    int events_fd, lock_fd, target_fd, gui, attempted;
    char serial[NWIPE_SERIALNUMBER_LENGTH + 1];
    const char *scope;
    const char *action;
    int block, crypto, overwrite, ns_count;
    time_t started_at;
} dispatch;

static void json_string( FILE *out, const char *text )
{
    fputc('"',out);
    for( const unsigned char *p=(const unsigned char *)(text ? text : ""); *p; p++ )
    {
        if( *p=='"' || *p=='\\' ) { fputc('\\',out); fputc(*p,out); }
        else if( *p<32 || *p>=127 ) fprintf(out,"\\u%04x",*p);
        else fputc(*p,out);
    }
    fputc('"',out);
}

static const char *state_label( repxe_san_state state )
{
    switch(state) {
        case REPXE_SAN_IDLE: return "idle";
        case REPXE_SAN_BUSY: return "in_progress";
        case REPXE_SAN_SUCCESS: return "success";
        case REPXE_SAN_FAILED: return "failed";
        case REPXE_SAN_FROZEN: return "frozen";
        default: return "unknown";
    }
}

static int event( dispatch *d, const char *kind, const repxe_san_sample *sample, int attempted, const char *pdf )
{
    /* Each complete record is flushed+fsync'd; O_EXCL prevents journal reuse. */
    char identity[sizeof(d->serial)+8]; snprintf(identity,sizeof(identity),"serial:%s",d->serial);
    char *buffer=NULL; size_t len=0;
    FILE *out=open_memstream(&buffer,&len);
    if(!out) return -1;
    fprintf(out,"{\"schema\":1,\"engine\":\"nwipe-0.43-repxe1\",\"time\":%lld,\"event\":",(long long)time(NULL));
    json_string(out,kind); fputs(",\"device\":",out); json_string(out,d->ctx->device_name);
    fputs(",\"serial\":",out); json_string(out,d->serial);
    fprintf(out,",\"size_bytes\":%llu,\"action\":",(unsigned long long)d->ctx->device_size);
    json_string(out,d->action);
    fputs(",\"engine_version\":\"0.43-repxe1\",\"protocol\":",out);
    json_string(out,d->ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_ATA?"ATA":d->ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_NVME?"NVMe":"unknown");
    fputs(",\"target_identity\":",out); json_string(out,identity);
    fputs(",\"native_method\":",out);
    if(!strcmp(d->action,"probe")) fputs("null",out);
    else { char method[32]; snprintf(method,sizeof(method),"sanitize_%s",d->action); json_string(out,method); }
    fputs(",\"methods\":[",out);
    int sep=0;
    if(d->block) { json_string(out,"sanitize_block"); sep=1; }
    if(d->crypto) { if(sep) fputc(',',out); json_string(out,"sanitize_crypto"); sep=1; }
    if(d->overwrite) { if(sep) fputc(',',out); json_string(out,"sanitize_overwrite"); }
    fputs("],\"controller_state\":",out);
    json_string(out,sample->state==REPXE_SAN_SUCCESS?"completed":state_label(sample->state));
    fprintf(out,",\"operation_started\":%s,\"scope_targets\":[{\"path\":",attempted?"true":"false");
    json_string(out,d->ctx->device_name); fputs(",\"identity\":",out); json_string(out,identity);
    fprintf(out,",\"size_bytes\":%llu}]",(unsigned long long)d->ctx->device_size);
    fputs(",\"controller_scope\":",out); json_string(out,d->scope);
    fprintf(out,",\"namespace_count\":%d,\"capabilities\":{\"block\":%s,\"crypto\":%s,\"overwrite\":%s},\"state\":",d->ns_count,
        d->block?"true":"false",d->crypto?"true":"false",d->overwrite?"true":"false");
    json_string(out,state_label(sample->state));
    fprintf(out,",\"command_attempted\":%s,\"progress_pct\":%d,\"remaining_seconds\":",attempted?"true":"false",sample->progress);
    if(sample->eta<0) fputs("null",out); else fprintf(out,"%d",sample->eta);
    fprintf(out,",\"elapsed_seconds\":%lld,\"pdf\":",d->started_at?(long long)(time(NULL)-d->started_at):0);
    if(pdf && pdf[0]) json_string(out,pdf); else fputs("null",out);
    fputs("}\n",out); fclose(out);
    size_t done=0;
    while(done<len) {
        ssize_t n=write(d->events_fd,buffer+done,len-done);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) { free(buffer); return -1; }
        done+=(size_t)n;
    }
    free(buffer); return fsync(d->events_fd);
}

static void paint( dispatch *d, const char *kind, const repxe_san_sample *sample )
{
    if(!d->gui) return;
    nwipe_gui_create_all_windows_on_terminal_resize(0,"CTRL+C stops monitoring; it does not stop drive firmware.","Do not reboot or remove power while sanitize is active or its outcome is unknown.");
    werase(options_window); box(options_window,0,0); nwipe_gui_title(options_window,"Sanitize method");
    mvwprintw(options_window,1,2,"%s",d->action); wnoutrefresh(options_window);
    werase(stats_window); box(stats_window,0,0); nwipe_gui_title(stats_window,"Progress");
    mvwprintw(stats_window,1,2,"%d%%   elapsed %llds",sample->progress,d->started_at?(long long)(time(NULL)-d->started_at):0);
    if(sample->eta>=0) mvwprintw(stats_window,2,2,"Estimated remaining %ds",sample->eta);
    else mvwprintw(stats_window,2,2,"Remaining time unavailable");
    wnoutrefresh(stats_window);
    werase(main_window); box(main_window,0,0); nwipe_gui_title(main_window,"Nwipe native sanitize - unattended routine");
    mvwprintw(main_window,2,2,"%s  %s",d->ctx->device_name,d->ctx->device_model);
    mvwprintw(main_window,3,2,"Serial: %s   Capacity: %llu bytes",d->serial,(unsigned long long)d->ctx->device_size);
    mvwprintw(main_window,4,2,"Scope: %s",d->scope);
    mvwprintw(main_window,6,2,"Status: %s (%s)",state_label(sample->state),kind);
    int width=getmaxx(main_window)-6;
    if(width>60) width=60;
    if(width>0) {
        mvwaddch(main_window,8,2,'[');
        for(int i=0;i<width;i++) mvwaddch(main_window,8,3+i,i<width*sample->progress/100?'#':' ');
        mvwaddch(main_window,8,3+width,']');
    }
    mvwprintw(main_window,10,2,"One firmware command. Status polling continues until confirmed completion.");
    mvwprintw(main_window,11,2,"Unknown or interrupted outcomes hold the routine; no erase retry or reboot.");
    wnoutrefresh(main_window); update_panels(); doupdate();
}

static int observe( void *opaque, const char *kind, const repxe_san_sample *sample, int attempted )
{
    dispatch *d=opaque; d->attempted=attempted;
    paint(d,kind,sample);
    return event(d,kind,sample,attempted,NULL);
}
static int interrupted( void *opaque ) {
    dispatch *d=opaque;
    if(d->gui) { timeout(0); int key=getch(); timeout(-1); if(key==3) return 1; }
    return terminate_signal==1;
}
static void delay_poll( void *opaque ) { (void)opaque; struct timespec pause={1,0}; nanosleep(&pause,NULL); }

static int poll_status( void *opaque, repxe_san_sample *sample )
{
    dispatch *d=opaque;
    sample->eta=-1; sample->action_matches=1;
    if(d->ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_ATA) {
        nwipe_se_ata_ctx *san=d->ctx->secure_erase_context;
        int rc=nwipe_se_ata_poll(san); if(rc) return rc;
        sample->progress=san->progress_pct;
        switch(san->state) {
            case NWIPE_SE_ATA_STATE_SUCCESS: sample->state=REPXE_SAN_SUCCESS; break;
            case NWIPE_SE_ATA_STATE_IN_PROGRESS: sample->state=REPXE_SAN_BUSY; break;
            case NWIPE_SE_ATA_STATE_FAILURE: sample->state=REPXE_SAN_FAILED; break;
            case NWIPE_SE_ATA_STATE_FROZEN: sample->state=REPXE_SAN_FROZEN; break;
            default: sample->state=REPXE_SAN_UNKNOWN;
        }
        /* ATA status does not echo the action; dispatch acceptance binds it. */
        return 0;
    }
#ifdef HAVE_LIBNVME
    nwipe_se_nvme_ctx *san=d->ctx->secure_erase_context;
    int rc=nwipe_se_nvme_poll(san); if(rc) return rc;
    sample->progress=san->progress_pct;
    sample->action_matches=(!d->attempted || san->sanact==san->planned_sanact);
    switch(san->state) {
        case NWIPE_SE_NVME_STATE_NEVER_SANITIZED: sample->state=REPXE_SAN_IDLE; break;
        case NWIPE_SE_NVME_STATE_SUCCESS: sample->state=REPXE_SAN_SUCCESS; break;
        case NWIPE_SE_NVME_STATE_IN_PROGRESS: sample->state=REPXE_SAN_BUSY; break;
        case NWIPE_SE_NVME_STATE_FAILURE: sample->state=REPXE_SAN_FAILED; break;
        default: sample->state=REPXE_SAN_UNKNOWN;
    }
    __u32 estimate=!strcmp(d->action,"block")?san->est_block_erase:!strcmp(d->action,"crypto")?san->est_crypto_erase:san->est_overwrite;
    if(sample->state==REPXE_SAN_SUCCESS) sample->eta=0;
    else if(estimate && estimate<INT_MAX) {
        time_t elapsed=d->started_at?time(NULL)-d->started_at:0;
        /* A total estimate that is already exceeded is unavailable, not zero. */
        if(estimate>(__u32)elapsed) sample->eta=(int)(estimate-elapsed);
    }
    return 0;
#else
    return -1;
#endif
}

static int start_once( void *opaque )
{
    dispatch *d=opaque;
    d->started_at=time(NULL); time(&d->ctx->start_time);
    d->ctx->secure_erase_status=NWIPE_SECURE_ERASE_STATUS_IN_PROGRESS;
    if(d->ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_ATA)
        return nwipe_se_ata_sanitize(d->ctx->secure_erase_context);
#ifdef HAVE_LIBNVME
    return nwipe_se_nvme_sanitize(d->ctx->secure_erase_context);
#else
    return -1;
#endif
}

#ifdef HAVE_LIBNVME
static int safe_nvme_scope( dispatch *d, nwipe_se_nvme_ctx *san )
{
    /* 1 means confirmed excluded scope, -1 means unknown/unbound scope. */
    struct nvme_id_ctrl id={0}; struct nvme_ns_list active={0}, allocated={0}; __u32 target_nsid=0;
    if(san->ns_count<1 || nvme_identify_ctrl(san->fd,&id)) return -1;
    if(san->ns_count>1 || (id.cmic & 0x07)) return 1;
    if(nvme_get_nsid(d->target_fd,&target_nsid) || !target_nsid
       || nvme_identify_active_ns_list(san->fd,0,&active)) return -1;
    if(le32toh(active.ns[0])!=target_nsid) return -1;
    if(active.ns[1]) return 1;
    /* Bind the controller's identify serial independently to the namespace. */
    char controller_serial[21]; memcpy(controller_serial,id.sn,20); controller_serial[20]=0;
    for(int n=19;n>=0 && controller_serial[n]==' ';n--) controller_serial[n]=0;
    if(strcmp(controller_serial,d->serial)) return -1;
    if(le16toh(id.oacs) & NVME_CTRL_OACS_NS_MGMT) {
        if(nvme_identify_allocated_ns_list(san->fd,0,&allocated)) return -1;
        if(le32toh(allocated.ns[0])!=target_nsid) return -1;
        if(allocated.ns[1]) return 1;
    } else {
        if(!le32toh(id.nn)) return -1;
        if(le32toh(id.nn)!=1) return 1;
    }
    d->scope=san->ctrl_path; d->ns_count=1;
    return 0;
}
#endif

int nwipe_repxe_sanitize( nwipe_context_t **contexts, int count )
{
    if(count!=1) { nwipe_log(NWIPE_LOG_ERROR,"Sanitize requires exactly one explicit whole device."); return 22; }
    nwipe_context_t *ctx=contexts[0];
    dispatch d={.ctx=ctx,.events_fd=-1,.lock_fd=-1,.target_fd=-1,.scope=ctx->device_name,.action=nwipe_options.repxe_sanitize};
    repxe_san_sample sample={REPXE_SAN_UNKNOWN,0,0,-1}; int result=22;
    memcpy(d.serial,ctx->device_serial_no,sizeof(d.serial));
    if(ctx->device_busy || ctx->device_type==NWIPE_DEVICE_USB || ctx->device_part
       || !d.serial[0] || strcmp(d.serial,nwipe_options.repxe_sanitize_serial)
       || (unsigned long long)ctx->device_size!=nwipe_options.repxe_sanitize_size)
    { nwipe_log(NWIPE_LOG_ERROR,"Sanitize target identity/capacity/whole-device/transport binding rejected."); return 22; }
    d.events_fd=open(nwipe_options.repxe_sanitize_events,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(d.events_fd<0) { nwipe_log(NWIPE_LOG_ERROR,"Sanitize journal must be a new writable file."); return 22; }
    d.target_fd=open(ctx->device_name,O_RDONLY|O_EXCL|O_NOFOLLOW|O_CLOEXEC);
    struct stat current={0}; unsigned long long current_size=0;
    if(d.target_fd<0 || fstat(d.target_fd,&current)
       || !repxe_binding_same_device(&ctx->device_stat,&current) || ioctl(d.target_fd,BLKGETSIZE64,&current_size)
       || current_size!=nwipe_options.repxe_sanitize_size)
        goto done;
    if(ctx->secure_erase_supported==0 || (ctx->secure_erase_supported==-2 && ctx->device_type==NWIPE_DEVICE_VIRT)) { result=20; sample.state=REPXE_SAN_IDLE; goto done; }
    if(ctx->secure_erase_supported!=1 || !ctx->secure_erase_context) goto done;
    if(ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_ATA) {
        nwipe_se_ata_ctx *san=ctx->secure_erase_context;
        if(nwipe_se_ata_open(san) || nwipe_se_ata_sancap(san) || !san->cap_caps_valid) goto done;
        d.block=san->cap_block_erase; d.crypto=san->cap_crypto_erase; d.overwrite=san->cap_overwrite;
        san->planned_sanact=!strcmp(d.action,"block")?NWIPE_SE_ATA_SANACT_BLOCK_ERASE:
            !strcmp(d.action,"crypto")?NWIPE_SE_ATA_SANACT_CRYPTO_SCRAMBLE:NWIPE_SE_ATA_SANACT_OVERWRITE;
        san->destructive_sanact=1; san->owpass=0; san->ovrpat=0;
    }
#ifdef HAVE_LIBNVME
    else if(ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_NVME) {
        nwipe_se_nvme_ctx *san=ctx->secure_erase_context;
        if(nwipe_se_nvme_open(san) || nwipe_se_nvme_sancap(san) || !san->cap_caps_valid) goto done;
        if(poll_status(&d,&sample) || sample.state==REPXE_SAN_BUSY || sample.state==REPXE_SAN_UNKNOWN) goto done;
        int scope_result=safe_nvme_scope(&d,san);
        if(scope_result) { result=scope_result>0?20:22; goto done; }
        d.block=san->cap_block_erase; d.crypto=san->cap_crypto_erase; d.overwrite=san->cap_overwrite;
        san->planned_sanact=!strcmp(d.action,"block")?NVME_SANITIZE_SANACT_START_BLOCK_ERASE:
            !strcmp(d.action,"crypto")?NVME_SANITIZE_SANACT_START_CRYPTO_ERASE:NVME_SANITIZE_SANACT_START_OVERWRITE;
        san->destructive_sanact=1; san->owpass=0; san->oipbp=false; san->ovrpat=0;
    }
#endif
    else goto done;
    /* A scope lock prevents parallel namespace/drive sessions dispatching the same controller. */
    char lockname[PATH_MAX]; const char *basename=strrchr(d.scope,'/'); basename=basename?basename+1:d.scope;
    snprintf(lockname,sizeof(lockname),"/run/nwipe-sanitize-%s.lock",basename);
    d.lock_fd=open(lockname,O_WRONLY|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0600);
    if(d.lock_fd<0 || flock(d.lock_fd,LOCK_EX|LOCK_NB)) goto done;
    if(!strcmp(d.action,"probe")) {
        result=poll_status(&d,&sample)?22:0;
        if(sample.state!=REPXE_SAN_IDLE && sample.state!=REPXE_SAN_SUCCESS) result=22;
        if(event(&d,"probe",&sample,0,NULL)) result=22;
        goto done;
    }
    if(poll_status(&d,&sample) || sample.state==REPXE_SAN_BUSY || sample.state==REPXE_SAN_UNKNOWN) goto done;
    if((!strcmp(d.action,"block")&&!d.block)||(!strcmp(d.action,"crypto")&&!d.crypto)||(!strcmp(d.action,"overwrite")&&!d.overwrite)) { result=20; goto done; }
    ctx->secure_erase_method=!strcmp(d.action,"block")?NWIPE_SECURE_ERASE_METHOD_BLOCK:
        !strcmp(d.action,"crypto")?NWIPE_SECURE_ERASE_METHOD_CRYPTO:NWIPE_SECURE_ERASE_METHOD_OVERWRITE;
    ctx->secure_erase_orchestration=NWIPE_SECURE_ERASE_ORCHESTRATION_STANDALONE;
    ctx->prng=nwipe_options.prng;
    if(!nwipe_options.nogui) { nwipe_gui_init(); d.gui=1; }
    repxe_san_backend backend={&d,poll_status,start_once,interrupted,observe,delay_poll};
    result=repxe_san_run(&backend);
    if(d.attempted) {
        calculate_duration_string(ctx);
        ctx->secure_erase_status=result==0?NWIPE_SECURE_ERASE_STATUS_SUCCESS:
            result==21?NWIPE_SECURE_ERASE_STATUS_FAILURE:NWIPE_SECURE_ERASE_STATUS_UNKNOWN;
        ctx->result=result; ctx->wipe_status=0;
        sample.state=result==0?REPXE_SAN_SUCCESS:result==21?REPXE_SAN_FAILED:REPXE_SAN_UNKNOWN;
        sample.progress=result==0?100:0; sample.eta=result==0?0:-1;
        /* The native generator retains its original SMART pages and visual design.
         * No-success outcomes remain failures in its standalone report context. */
        create_single_disc_pdf(global_nwipe_thread_data_ptr,ctx);
        FILE *pdf=fopen(ctx->PDF_filename,"rb"); char magic[6]={0};
        int valid=pdf && fread(magic,1,5,pdf)==5 && !memcmp(magic,"%PDF-",5);
        if(pdf) fclose(pdf);
        if(result==0 && !valid) { result=22; sample.state=REPXE_SAN_UNKNOWN; }
        if(event(&d,result==0?"complete":result==21?"failed":"hold",&sample,1,valid?ctx->PDF_filename:NULL)) result=22;
        paint(&d,result==0?"complete":result==21?"failed":"hold",&sample);
    }
 done:
    if(result==22 && !d.attempted) { sample.state=REPXE_SAN_UNKNOWN; event(&d,"hold",&sample,0,NULL); }
    if(result==20) { event(&d,"unsupported_or_refused",&sample,0,NULL); nwipe_log(NWIPE_LOG_ERROR,"Sanitize unsupported or scope/identity preflight refused; no destructive command issued."); }
    if(d.gui) nwipe_gui_free();
    if(ctx->secure_erase_context) {
        if(ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_ATA) nwipe_se_ata_close(ctx->secure_erase_context);
#ifdef HAVE_LIBNVME
        else if(ctx->secure_erase_type==NWIPE_SECURE_ERASE_TYPE_NVME) nwipe_se_nvme_close(ctx->secure_erase_context);
#endif
    }
    if(d.target_fd>=0) close(d.target_fd);
    if(d.lock_fd>=0) close(d.lock_fd);
    close(d.events_fd); return result;
}
