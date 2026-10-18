// BLE board-to-board link (CG_GATEWAY_NODE_LINK=1). Each node (torso, left wrist) is a small GATT
// server with a player- and role-specific service UUID, so a hub only ever finds its own player's
// nodes. The hub, which is already the relay's peripheral, is also their central: it scans, connects,
// subscribes to the node's notify characteristic and writes to its write characteristic. A packet
// on the air is [tag][sketch packet], the tag standing in for the UDP port (gateway_link.h).
#include <Arduino.h>
#include "gateway_link.h"

#if CG_GATEWAY_NODE_LINK
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <esp_bt.h>
#include <esp_gap_ble_api.h>
#include <esp_gattc_api.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

namespace {
struct LinkPacket {
    uint8_t len;
    uint8_t data[GATEWAY_LINK_MAX_PACKET];
};
QueueHandle_t rxQueues[GATEWAY_LINK_TAGS];

// Runs on the BLE task. A full queue drops its oldest packet: these are periodic state packets
// where the newest matters (SegmentRequest/Ack already retry on their own).
void deliver(const uint8_t *value, size_t len) {
    if (len < 2 || len - 1 > GATEWAY_LINK_MAX_PACKET || value[0] >= GATEWAY_LINK_TAGS) return;
    QueueHandle_t q = rxQueues[value[0]];
    if (!q) return; // a tag this board never opened (e.g. LINK on the left wrist)
    LinkPacket p;
    p.len = (uint8_t)(len - 1);
    memcpy(p.data, value + 1, p.len);
    if (xQueueSend(q, &p, 0) != pdTRUE) {
        LinkPacket oldest;
        xQueueReceive(q, &oldest, 0);
        xQueueSend(q, &p, 0);
    }
}

// 4347 = "CG", then player and role; 6c69-6e6b = "link"; last byte 0 service, 1 node->hub, 2 hub->node.
std::string linkUuid(uint8_t role, uint8_t suffix) {
    char s[37];
    snprintf(s, sizeof(s), "4347%02x%02x-6c69-6e6b-8000-0000000000%02x", CG_GATEWAY_PLAYER, role, suffix);
    return s;
}

void printHeap(const char *when) {
    Serial.printf("[link] free heap %s: %u (largest block %u)\n", when, (unsigned)ESP.getFreeHeap(),
                  (unsigned)ESP.getMaxAllocHeap());
}
} // namespace

bool gatewayLinkOpen(uint8_t tag) {
    if (tag >= GATEWAY_LINK_TAGS) return false;
    if (!rxQueues[tag]) rxQueues[tag] = xQueueCreate(16, sizeof(LinkPacket));
    return rxQueues[tag] != nullptr;
}

bool gatewayLinkTake(uint8_t tag, uint8_t *data, size_t &len) {
    LinkPacket p;
    if (tag >= GATEWAY_LINK_TAGS || !rxQueues[tag] || xQueueReceive(rxQueues[tag], &p, 0) != pdTRUE)
        return false;
    memcpy(data, p.data, p.len);
    len = p.len;
    return true;
}

#if CG_GATEWAY_ROLE == 0
// ---------------- Hub: central to its torso and left wrist ----------------
namespace {
constexpr uint16_t kAppId = 0x4c; // BLEServer registers app 0
constexpr uint32_t kSetupTimeoutMs = 10000;

struct Node {
    uint8_t role;
    const char *name;
    esp_bt_uuid_t service, toHub, fromHub;
    esp_bd_addr_t bda;
    uint16_t connId, start, end, rxHandle, txHandle;
    bool connected;
    volatile bool ready;
};
Node nodes[2] = {{1, "torso"}, {2, "left_wrist"}};

esp_gatt_if_t gattcIf = ESP_GATT_IF_NONE;
portMUX_TYPE linkMux = portMUX_INITIALIZER_UNLOCKED;
bool scanParamsSet, scanning;
int pending = -1; // node being connected/set up; one at a time (both share one GATT layout)
uint32_t pendingSince;

Node *nodeByConn(uint16_t connId) {
    for (auto &n : nodes)
        if (n.connected && n.connId == connId) return &n;
    return nullptr;
}

void maybeScan() {
    portENTER_CRITICAL(&linkMux);
    bool missing = !nodes[0].connected || !nodes[1].connected;
    bool go = missing && gattcIf != ESP_GATT_IF_NONE && scanParamsSet && !scanning && pending < 0;
    if (go) scanning = true;
    portEXIT_CRITICAL(&linkMux);
    if (go && esp_ble_gap_start_scanning(0) != ESP_OK) {
        portENTER_CRITICAL(&linkMux);
        scanning = false;
        portEXIT_CRITICAL(&linkMux);
    }
}

void setupDone(Node &n, bool ok) {
    if (ok) {
        n.ready = true;
        Serial.printf("[link] %s connected\n", n.name);
    } else {
        Serial.printf("[link] %s setup failed, dropping the connection\n", n.name);
        esp_ble_gap_disconnect(n.bda); // the physical link; DISCONNECT_EVT then rescans
    }
    portENTER_CRITICAL(&linkMux);
    pending = -1;
    portEXIT_CRITICAL(&linkMux);
    maybeScan();
}

void onScanResult(esp_ble_gap_cb_param_t::ble_scan_result_evt_param &r) {
    uint8_t len = 0;
    uint8_t *uuid = esp_ble_resolve_adv_data(r.ble_adv, ESP_BLE_AD_TYPE_128SRV_CMPL, &len);
    if (!uuid || len != 16) uuid = esp_ble_resolve_adv_data(r.ble_adv, ESP_BLE_AD_TYPE_128SRV_PART, &len);
    if (!uuid || len != 16) return;
    int found = -1;
    for (int i = 0; i < 2; i++)
        if (!nodes[i].connected && memcmp(uuid, nodes[i].service.uuid.uuid128, 16) == 0) found = i;
    if (found < 0) return;
    bool go = false;
    portENTER_CRITICAL(&linkMux);
    if (pending < 0 && scanning) {
        pending = found;
        pendingSince = millis();
        scanning = false;
        go = true;
    }
    portEXIT_CRITICAL(&linkMux);
    if (!go) return;
    esp_ble_gap_stop_scanning();
    Node &n = nodes[found];
    memcpy(n.bda, r.bda, sizeof(n.bda));
    // 15-30 ms, like the relay link: three links share the hub's radio, and the torso beacon is 50 Hz.
    esp_ble_gap_set_prefer_conn_params(r.bda, 0x0C, 0x18, 0, 400);
    if (esp_ble_gattc_open(gattcIf, r.bda, r.ble_addr_type, true) != ESP_OK) {
        portENTER_CRITICAL(&linkMux);
        pending = -1;
        portEXIT_CRITICAL(&linkMux);
        maybeScan();
    }
}

void gapHandler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param) {
    switch (event) {
    case ESP_GAP_BLE_SCAN_PARAM_SET_COMPLETE_EVT:
        scanParamsSet = param->scan_param_cmpl.status == ESP_BT_STATUS_SUCCESS;
        Serial.println("[link] scanning for torso and left wrist");
        maybeScan();
        break;
    case ESP_GAP_BLE_SCAN_START_COMPLETE_EVT:
        if (param->scan_start_cmpl.status != ESP_BT_STATUS_SUCCESS) {
            portENTER_CRITICAL(&linkMux);
            scanning = false;
            portEXIT_CRITICAL(&linkMux);
        }
        break;
    case ESP_GAP_BLE_SCAN_RESULT_EVT:
        if (param->scan_rst.search_evt == ESP_GAP_SEARCH_INQ_RES_EVT) onScanResult(param->scan_rst);
        else if (param->scan_rst.search_evt == ESP_GAP_SEARCH_INQ_CMPL_EVT) {
            portENTER_CRITICAL(&linkMux);
            scanning = false;
            portEXIT_CRITICAL(&linkMux);
            maybeScan();
        }
        break;
    default:
        break;
    }
}

void gattcHandler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if, esp_ble_gattc_cb_param_t *p) {
    if (event == ESP_GATTC_REG_EVT) {
        if (p->reg.app_id != kAppId) return;
        if (p->reg.status != ESP_GATT_OK) {
            Serial.printf("[link] GATT client registration failed (%d)\n", p->reg.status);
            return;
        }
        gattcIf = gattc_if;
        esp_ble_scan_params_t params = {};
        params.scan_type = BLE_SCAN_TYPE_PASSIVE; // the service UUID is in the advertisement itself
        params.own_addr_type = BLE_ADDR_TYPE_PUBLIC;
        params.scan_filter_policy = BLE_SCAN_FILTER_ALLOW_ALL;
        // Only while a node is missing. 20 ms of every 80 ms leaves the radio to the relay link; nodes
        // advertise every 20-40 ms, so a powered node is still found within a few hundred ms.
        params.scan_interval = 0x80;
        params.scan_window = 0x20;
        params.scan_duplicate = BLE_SCAN_DUPLICATE_DISABLE;
        esp_ble_gap_set_scan_params(&params);
        return;
    }
    if (gattc_if != gattcIf) return;
    switch (event) {
    case ESP_GATTC_OPEN_EVT: {
        portENTER_CRITICAL(&linkMux);
        int i = pending;
        portEXIT_CRITICAL(&linkMux);
        if (i < 0 || memcmp(p->open.remote_bda, nodes[i].bda, sizeof(esp_bd_addr_t)) != 0) break;
        Node &n = nodes[i];
        if (p->open.status != ESP_GATT_OK) {
            Serial.printf("[link] %s connect failed (%d), scanning again\n", n.name, p->open.status);
            portENTER_CRITICAL(&linkMux);
            pending = -1;
            portEXIT_CRITICAL(&linkMux);
            maybeScan();
            break;
        }
        n.connId = p->open.conn_id;
        n.start = n.end = n.rxHandle = n.txHandle = 0;
        n.connected = true;
        esp_ble_gattc_send_mtu_req(gattcIf, n.connId);
        esp_ble_gattc_search_service(gattcIf, n.connId, &n.service);
        break;
    }
    case ESP_GATTC_SEARCH_RES_EVT: {
        Node *n = nodeByConn(p->search_res.conn_id);
        if (n && p->search_res.srvc_id.uuid.len == ESP_UUID_LEN_128 &&
            memcmp(p->search_res.srvc_id.uuid.uuid.uuid128, n->service.uuid.uuid128, 16) == 0) {
            n->start = p->search_res.start_handle;
            n->end = p->search_res.end_handle;
        }
        break;
    }
    case ESP_GATTC_SEARCH_CMPL_EVT: {
        Node *n = nodeByConn(p->search_cmpl.conn_id);
        if (!n) break;
        esp_gattc_char_elem_t c;
        uint16_t count = 1;
        if (n->start && esp_ble_gattc_get_char_by_uuid(gattcIf, n->connId, n->start, n->end, n->toHub, &c,
                                                       &count) == ESP_GATT_OK && count)
            n->rxHandle = c.char_handle;
        count = 1;
        if (n->start && esp_ble_gattc_get_char_by_uuid(gattcIf, n->connId, n->start, n->end, n->fromHub, &c,
                                                       &count) == ESP_GATT_OK && count)
            n->txHandle = c.char_handle;
        if (!n->rxHandle || !n->txHandle) setupDone(*n, false);
        else esp_ble_gattc_register_for_notify(gattcIf, n->bda, n->rxHandle);
        break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
        // Carries only a handle, and both nodes share one layout: it belongs to the pending node.
        portENTER_CRITICAL(&linkMux);
        int i = pending;
        portEXIT_CRITICAL(&linkMux);
        if (i < 0 || !nodes[i].connected) break;
        Node &n = nodes[i];
        esp_bt_uuid_t cccdUuid = {};
        cccdUuid.len = ESP_UUID_LEN_16;
        cccdUuid.uuid.uuid16 = ESP_GATT_UUID_CHAR_CLIENT_CONFIG;
        esp_gattc_descr_elem_t d;
        uint16_t count = 1;
        if (p->reg_for_notify.status != ESP_GATT_OK ||
            esp_ble_gattc_get_descr_by_char_handle(gattcIf, n.connId, n.rxHandle, cccdUuid, &d, &count) !=
                ESP_GATT_OK || !count) {
            setupDone(n, false);
            break;
        }
        uint8_t enable[2] = {1, 0};
        esp_ble_gattc_write_char_descr(gattcIf, n.connId, d.handle, sizeof(enable), enable,
                                       ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);
        break;
    }
    case ESP_GATTC_WRITE_DESCR_EVT: {
        Node *n = nodeByConn(p->write.conn_id);
        if (n && !n->ready) setupDone(*n, p->write.status == ESP_GATT_OK);
        break;
    }
    case ESP_GATTC_NOTIFY_EVT:
        if (nodeByConn(p->notify.conn_id)) deliver(p->notify.value, p->notify.value_len);
        break;
    case ESP_GATTC_DISCONNECT_EVT: {
        Node *n = nodeByConn(p->disconnect.conn_id);
        if (!n) break;
        n->ready = false;
        n->connected = false;
        Serial.printf("[link] %s disconnected (reason 0x%x), scanning again\n", n->name, p->disconnect.reason);
        portENTER_CRITICAL(&linkMux);
        if (pending == (int)(n - nodes)) pending = -1;
        portEXIT_CRITICAL(&linkMux);
        maybeScan();
        break;
    }
    default:
        break;
    }
}
} // namespace

// The sketch's setup() runs before the hub's BLE stack exists (the relay transport task starts it):
// nothing to do there. ble_setup() calls gatewayHubLinkBegin() once the relay server is up.
void gatewayNodeLinkStart() { printHeap("at boot, no Wi-Fi"); }

void gatewayHubLinkBegin() {
    for (auto &n : nodes) {
        n.service = *BLEUUID(linkUuid(n.role, 0)).getNative();
        n.toHub = *BLEUUID(linkUuid(n.role, 1)).getNative();
        n.fromHub = *BLEUUID(linkUuid(n.role, 2)).getNative();
    }
    BLEDevice::setCustomGapHandler(gapHandler);
    BLEDevice::setCustomGattcHandler(gattcHandler);
    if (esp_ble_gattc_app_register(kAppId) != ESP_OK) Serial.println("[link] GATT client registration failed");
}

// Hub loop: restart a scan that stopped, and give up on a node connected but stuck half set up.
// (A connect that never completes is reported by Bluedroid itself as a failed OPEN_EVT.)
void gatewayLinkService() {
    static uint32_t lastCheck;
    uint32_t now = millis();
    if (now - lastCheck < 1000) return;
    lastCheck = now;
    portENTER_CRITICAL(&linkMux);
    int i = pending;
    bool stuck = i >= 0 && now - pendingSince > kSetupTimeoutMs;
    portEXIT_CRITICAL(&linkMux);
    if (stuck && nodes[i].connected && !nodes[i].ready) setupDone(nodes[i], false);
    maybeScan();
}

void gatewayLinkSend(uint8_t tag, const uint8_t *data, size_t len) {
    if (gattcIf == ESP_GATT_IF_NONE || len > GATEWAY_LINK_MAX_PACKET) return;
    uint8_t buf[GATEWAY_LINK_MAX_PACKET + 1];
    buf[0] = tag;
    memcpy(buf + 1, data, len);
    for (auto &n : nodes) {
        // LINK_PORT was hub<->torso only; POSE (HubStatus) goes to both nodes.
        if (!n.ready || (tag == 0 && n.role != 1)) continue;
        esp_ble_gattc_write_char(gattcIf, n.connId, n.txHandle, len + 1, buf, ESP_GATT_WRITE_TYPE_NO_RSP,
                                 ESP_GATT_AUTH_REQ_NONE);
    }
}

#else
// ---------------- Node: peripheral the hub connects to ----------------
namespace {
BLECharacteristic *toHub;
BLE2902 *toHubCccd;
volatile bool hubConnected;

class NodeServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer *) override {
        hubConnected = true;
        Serial.println("[link] hub connected");
    }
    void onDisconnect(BLEServer *server) override {
        hubConnected = false;
        toHubCccd->setNotifications(false); // not cleared by Bluedroid for an unbonded central
        Serial.println("[link] hub disconnected, advertising again");
        server->startAdvertising();
    }
};

class FromHubCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *c) override {
        std::string v = c->getValue();
        deliver((const uint8_t *)v.data(), v.length());
    }
};
} // namespace

void gatewayNodeLinkStart() {
    esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT); // BLE only; allowed only before init
    printHeap("before BLE init");
    char name[16];
    snprintf(name, sizeof(name), "CG-P%u-%s", CG_GATEWAY_PLAYER, CG_GATEWAY_ROLE == 1 ? "TORSO" : "LEFT");
    BLEDevice::init(name);
    BLEDevice::setMTU(64); // room for a continuous-mode sample (31 bytes) as well
    BLEServer *server = BLEDevice::createServer();
    server->setCallbacks(new NodeServerCallbacks());
    BLEService *service = server->createService(linkUuid(CG_GATEWAY_ROLE, 0));
    toHub = service->createCharacteristic(linkUuid(CG_GATEWAY_ROLE, 1), BLECharacteristic::PROPERTY_NOTIFY);
    toHubCccd = new BLE2902();
    toHub->addDescriptor(toHubCccd);
    BLECharacteristic *fromHub = service->createCharacteristic(
        linkUuid(CG_GATEWAY_ROLE, 2), BLECharacteristic::PROPERTY_WRITE_NR | BLECharacteristic::PROPERTY_WRITE);
    fromHub->setCallbacks(new FromHubCallbacks());
    service->start();
    BLEAdvertising *advertising = BLEDevice::getAdvertising();
    advertising->addServiceUUID(linkUuid(CG_GATEWAY_ROLE, 0));
    advertising->start();
    printHeap("after BLE init");
    Serial.printf("[link] advertising as %s, waiting for the hub\n", name);
}

void gatewayHubLinkBegin() {}
void gatewayLinkService() {}

void gatewayLinkSend(uint8_t tag, const uint8_t *data, size_t len) {
    if (!hubConnected || !toHubCccd->getNotifications() || len > GATEWAY_LINK_MAX_PACKET) return;
    uint8_t buf[GATEWAY_LINK_MAX_PACKET + 1];
    buf[0] = tag;
    memcpy(buf + 1, data, len);
    toHub->setValue(buf, len + 1);
    toHub->notify();
}
#endif

#else
bool gatewayLinkOpen(uint8_t) { return false; }
bool gatewayLinkTake(uint8_t, uint8_t *, size_t &) { return false; }
void gatewayLinkSend(uint8_t, const uint8_t *, size_t) {}
void gatewayNodeLinkStart() {}
void gatewayHubLinkBegin() {}
void gatewayLinkService() {}
#endif
