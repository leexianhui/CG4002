// Reuse the tested comms transport; only the I/O hooks differ from the demo.
#if CG_GATEWAY_ROLE == 0
#include <BLEDevice.h>
#include "cg_frame.h"
#define setup gatewayTransportSetup
#define loop gatewayTransportLoop
#include "../../firmware_demo/src/main.cpp"
#undef setup
#undef loop
#endif
