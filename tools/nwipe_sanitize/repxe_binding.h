#ifndef REPXE_BINDING_H
#define REPXE_BINDING_H
#include <sys/stat.h>

/* lstat never follows a symlink. A failed stat cannot leave usable evidence. */
static inline int repxe_binding_snapshot_valid( int status, const struct stat *snapshot )
{
    return status == 0 && snapshot && S_ISBLK( snapshot->st_mode ) && snapshot->st_rdev != 0;
}

/* Both snapshots must be independently valid, including the opened descriptor. */
static inline int repxe_binding_same_device( const struct stat *before, const struct stat *opened )
{
    return repxe_binding_snapshot_valid( 0, before ) && repxe_binding_snapshot_valid( 0, opened )
        && before->st_rdev == opened->st_rdev;
}
#endif
