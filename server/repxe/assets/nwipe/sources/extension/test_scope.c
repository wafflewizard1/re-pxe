/* Include the native gate only; unreferenced device/UI paths are linker-discarded.
 * libnvme identification APIs are mocked below. No path is opened or ioctl sent.
 */
#include "repxe_sanitize.c"
#include <assert.h>
static int controller_error, nsid_error, active_error, allocated_error;
static struct nvme_id_ctrl mock_id;
static struct nvme_ns_list mock_active, mock_allocated;
int nvme_identify(struct nvme_identify_args *args) {
    switch(args->cns) {
        case NVME_IDENTIFY_CNS_CTRL: memcpy(args->data,&mock_id,sizeof(mock_id)); return controller_error;
        case NVME_IDENTIFY_CNS_NS_ACTIVE_LIST: memcpy(args->data,&mock_active,sizeof(mock_active)); return active_error;
        case NVME_IDENTIFY_CNS_ALLOCATED_NS_LIST: memcpy(args->data,&mock_allocated,sizeof(mock_allocated)); return allocated_error;
        default: assert(0); return -1;
    }
}
int nvme_get_nsid(int fd,__u32 *nsid) { assert(fd==777); *nsid=1; return nsid_error; }
static void reset(dispatch *d,nwipe_se_nvme_ctx *san) {
    memset(&mock_id,0,sizeof(mock_id));memset(&mock_active,0,sizeof(mock_active));memset(&mock_allocated,0,sizeof(mock_allocated));
    memset(d,0,sizeof(*d));memset(san,0,sizeof(*san));
    controller_error=nsid_error=active_error=allocated_error=0;
    memcpy(mock_id.sn,"SAFE-001            ",20);mock_id.nn=htole32(1);
    mock_active.ns[0]=htole32(1);mock_allocated.ns[0]=htole32(1);
    strcpy(d->serial,"SAFE-001");d->target_fd=777;san->ns_count=1;san->ctrl_path="/dev/mock-controller";
}
int main(void) {
    int checks=0;dispatch d;nwipe_se_nvme_ctx san;
    reset(&d,&san);assert(safe_nvme_scope(&d,&san)==0 && d.ns_count==1);checks++;
    reset(&d,&san);controller_error=-1;assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);san.ns_count=2;assert(safe_nvme_scope(&d,&san)==1);checks++;
    reset(&d,&san);san.ns_count=0;assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);mock_id.cmic=2;assert(safe_nvme_scope(&d,&san)==1);checks++;
    reset(&d,&san);nsid_error=-1;assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);active_error=-1;assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);mock_active.ns[0]=htole32(2);assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);mock_active.ns[1]=htole32(2);assert(safe_nvme_scope(&d,&san)==1);checks++;
    reset(&d,&san);mock_id.sn[0]='X';assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);mock_id.oacs=htole16(NVME_CTRL_OACS_NS_MGMT);allocated_error=-1;assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);mock_id.oacs=htole16(NVME_CTRL_OACS_NS_MGMT);mock_allocated.ns[1]=htole32(2);assert(safe_nvme_scope(&d,&san)==1);checks++;
    reset(&d,&san);mock_id.oacs=htole16(NVME_CTRL_OACS_NS_MGMT);mock_allocated.ns[0]=htole32(2);assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);mock_id.nn=0;assert(safe_nvme_scope(&d,&san)==-1);checks++;
    reset(&d,&san);mock_id.nn=htole32(2);assert(safe_nvme_scope(&d,&san)==1);checks++;
    reset(&d,&san);mock_id.oacs=htole16(NVME_CTRL_OACS_NS_MGMT);assert(safe_nvme_scope(&d,&san)==0);checks++;
    printf("PASS: %d mocked NVMe scope cases; no physical devices opened\n",checks);
}
