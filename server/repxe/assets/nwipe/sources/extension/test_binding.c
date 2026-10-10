#define _GNU_SOURCE
#include <assert.h>
#include <stdio.h>
#include <sys/sysmacros.h>
#include "repxe_binding.h"

int main( void )
{
    struct stat disk = { .st_mode = S_IFBLK | 0600, .st_rdev = makedev( 8, 0 ) };
    struct stat opened = disk;
    assert( repxe_binding_snapshot_valid( 0, &disk ) );
    assert( !repxe_binding_snapshot_valid( -1, &disk ) );
    assert( !repxe_binding_snapshot_valid( 0, NULL ) );
    struct stat uninitialized = { 0 };
    assert( !repxe_binding_snapshot_valid( 0, &uninitialized ) );
    assert( !repxe_binding_same_device( &uninitialized, &disk ) );
    assert( repxe_binding_same_device( &disk, &opened ) );
    opened.st_rdev = makedev( 8, 16 );
    assert( !repxe_binding_same_device( &disk, &opened ) );
    opened = disk;
    opened.st_mode = S_IFLNK | 0777;
    assert( !repxe_binding_snapshot_valid( 0, &opened ) );
    assert( !repxe_binding_same_device( &disk, &opened ) );
    opened.st_mode = S_IFREG | 0600;
    assert( !repxe_binding_snapshot_valid( 0, &opened ) );
    opened.st_mode = S_IFCHR | 0600;
    assert( !repxe_binding_snapshot_valid( 0, &opened ) );
    opened = disk;
    opened.st_rdev = 0;
    assert( !repxe_binding_snapshot_valid( 0, &opened ) );
    puts( "12 mocked device-binding checks passed; no device opened" );
    return 0;
}
