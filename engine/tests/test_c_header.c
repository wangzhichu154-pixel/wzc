#include "trading/vector_core.h"

_Static_assert(sizeof(uint64_t) == 8, "64-bit ABI field");
_Static_assert(sizeof(uint32_t) == 4, "32-bit ABI field");

int main(void) {
    vc_handle* handle = NULL;
    vc_config config = {0};
    config.abi_version = VC_ABI_VERSION;
    config.struct_size = (uint32_t)sizeof(config);
    if (vc_create(&config, &handle) != VC_INVALID_ARGUMENT || handle != NULL)
        return 1;
    return vc_destroy(NULL) == VC_OK ? 0 : 1;
}
