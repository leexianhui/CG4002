#pragma once
#include <Arduino.h>
#include <IPAddress.h>

// Board-to-board link. 1 (default): the hub reaches its own torso and left wrist over BLE and no
// board starts Wi-Fi, so the hub's one radio only does BLE (relay + two nodes) and Wi-Fi's heap is
// free for the BLE stack. 0: the hardware team's original Wi-Fi UDP broadcast on the hotspot
// (their laptop listener / recording tools need it). Must match on all three boards of a player.
#ifndef CG_GATEWAY_NODE_LINK
#define CG_GATEWAY_NODE_LINK 1
#endif

// The sketches' UDP ports are 4210 + 10*(player-1) + k. Over BLE a packet carries k as a one-byte
// tag instead; the player is implicit because a hub only connects to its own player's nodes.
#define GATEWAY_LINK_PORT_BASE 4210
#define GATEWAY_LINK_TAGS 6
#define GATEWAY_LINK_MAX_PACKET 40 // largest is the 30-byte continuous-mode GatewaySample

inline int gatewayLinkTag(uint16_t port) {
    return port < GATEWAY_LINK_PORT_BASE ? -1 : (port - GATEWAY_LINK_PORT_BASE) % 10;
}
// LINK (0), POSE (2) and the continuous-mode sample port (5) go between boards. DEBUG/RESULT/RAW
// (1, 3, 4) were for the laptop listener; over BLE they are dropped (debugLog still prints Serial).
inline bool gatewayLinkCarries(int tag) { return tag == 0 || tag == 2 || tag == 5; }

bool gatewayLinkOpen(uint8_t tag);
bool gatewayLinkTake(uint8_t tag, uint8_t *data, size_t &len);
void gatewayLinkSend(uint8_t tag, const uint8_t *data, size_t len);
// Called where the sketch joined Wi-Fi. Nodes: start the BLE peripheral the hub connects to.
// Hub: nothing yet (its BLE stack comes up on the relay task, which then calls gatewayHubLinkBegin
// to find and connect its torso and left wrist). No-ops when CG_GATEWAY_NODE_LINK is 0.
void gatewayNodeLinkStart();
void gatewayHubLinkBegin();
// Hub loop housekeeping (rescan, give up on a stuck node setup). No-op elsewhere.
void gatewayLinkService();

// Drop-in for the sketches' WiFiUDP sockets: same calls, keyed by destination port.
class GatewayLinkUdp : public Print {
public:
    using Print::write;
    uint8_t begin(uint16_t port) {
        int tag = gatewayLinkTag(port);
        if (gatewayLinkCarries(tag) && !gatewayLinkOpen((uint8_t)tag)) return 0;
        rxTag = gatewayLinkCarries(tag) ? tag : -1;
        return 1;
    }
    int beginPacket(IPAddress, uint16_t port) {
        int tag = gatewayLinkTag(port);
        txTag = gatewayLinkCarries(tag) ? tag : -1;
        txLen = 0;
        return 1;
    }
    size_t write(uint8_t b) override { return write(&b, 1); }
    size_t write(const uint8_t *data, size_t size) override {
        if (txTag < 0) return size; // a laptop-only port: discard
        if (txLen + size > sizeof(tx)) { txTag = -1; return 0; }
        memcpy(tx + txLen, data, size);
        txLen += size;
        return size;
    }
    int endPacket() {
        if (txTag >= 0 && txLen) gatewayLinkSend((uint8_t)txTag, tx, txLen);
        txTag = -1;
        return 1;
    }
    int parsePacket() {
        rxPos = rxLen = 0;
        if (rxTag < 0 || !gatewayLinkTake((uint8_t)rxTag, rx, rxLen)) return 0;
        return (int)rxLen;
    }
    int read(uint8_t *data, size_t len) {
        size_t n = rxLen - rxPos < len ? rxLen - rxPos : len;
        memcpy(data, rx + rxPos, n);
        rxPos += n;
        return (int)n;
    }
    void flush() override { rxPos = rxLen = 0; }

private:
    int8_t rxTag = -1, txTag = -1;
    uint8_t tx[GATEWAY_LINK_MAX_PACKET], rx[GATEWAY_LINK_MAX_PACKET];
    size_t txLen = 0, rxLen = 0, rxPos = 0;
};
