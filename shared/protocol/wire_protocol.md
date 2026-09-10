# CG4002 Wire Protocol — FireBeetle ↔ Laptop

**Normative.** This is the single source of truth for the binary protocol between a wearable device
(wrist ESP32-C3 via the belt FireBeetle) and the laptop relay. It mirrors `cg_protocol.h` exactly — if
this document and the header ever disagree, that is a bug, not a choice; fix them together in the same
PR. See `../../docs/COMMS_IMPLEMENTATION_PLAN.md` §4 for design rationale.

Current protocol version: see `VERSION` in this directory (currently `1`).

All multi-byte integers are **little-endian**. Both ends of the link (ESP32, laptop) are little-endian
natively, so there is no byte swapping anywhere in this protocol.

---

## 1. Frame layout

| Offset | Field | Size (B) | Value |
|---|---|---|---|
| 0 | `MAGIC` | 1 | `0xC9` |
| 1 | `VERSION` | 1 | protocol version, currently `1` |
| 2 | `TYPE` | 1 | bit 7 = `MORE_FRAGMENTS`; bits 6–0 = packet type |
| 3 | `SEQ` | 2 | `uint16`, per-device **per-direction**, wraps at 65535 |
| 5 | `PLAYER_ID` | 1 | `1` or `2` (`0` = unassigned, only valid in `HELLO`) |
| 6 | `LEN` | 2 | `uint16`, byte length of `PAYLOAD` |
| 8 | `PAYLOAD` | `LEN` | ciphertext ‖ 16-byte GCM tag (§4) |
| 8 + `LEN` | `CRC16` | 2 | CRC-16/CCITT-FALSE over bytes `[0, 8+LEN)` |

Fixed overhead: 10 bytes (8-byte header + 2-byte CRC) + 16-byte GCM auth tag = **26 bytes per packet**.

`MORE_FRAGMENTS` lives in the high bit of `TYPE` rather than a separate field, so packet type values are
constrained to `0x00`–`0x7F`.

Verify `CRC16` first, then decrypt. The GCM tag already provides integrity; the CRC lets a receiver
discard a corrupted frame before spending a decrypt on it, which matters on the ESP32 side. Keep both.

---

## 2. Packet types

| Constant | Value | Direction | Delivery |
|---|---|---|---|
| `CG_PKT_HELLO` | `0x01` | device → laptop | reliable |
| `CG_PKT_HELLO_ACK` | `0x02` | laptop → device | reliable |
| `CG_PKT_TIME_SYNC_REQ` | `0x03` | device → laptop | reliable |
| `CG_PKT_TIME_SYNC_RESP` | `0x04` | laptop → device | reliable |
| `CG_PKT_SENSOR_FRAME` | `0x10` | device → laptop | best-effort |
| `CG_PKT_RFID_EVENT` | `0x11` | device → laptop | reliable |
| `CG_PKT_HEARTBEAT` | `0x20` | both | best-effort |
| `CG_PKT_ACK` | `0x21` | both | never acked itself |
| `CG_PKT_COMBAT_FEEDBACK` | `0x30` | laptop → device | reliable |

| Packet | Delivery | Rationale |
|---|---|---|
| `SENSOR_FRAME`, `HEARTBEAT` | best-effort | 50 Hz stream; a dropped frame shortens a window slightly |
| `HELLO`, `HELLO_ACK`, `TIME_SYNC_*`, `RFID_EVENT`, `COMBAT_FEEDBACK` | ACK + retransmit | state-changing; a lost weapon change desyncs the whole match |
| `ACK` | never acked | obviously |

---

## 3. Payloads (plaintext, before encryption)

All laptop-side timestamps are `uint32` milliseconds since `serverEpochUnixMs`, which the laptop sends
once in `HELLO_ACK`. Device timestamps are `uint32` milliseconds on the device's own clock.

```
HELLO                (16 B)  deviceMac[6] | playerId u8 | protocolVersion u8 |
                             firmwareVersion u16 | deviceNonce u32 | reserved u16
HELLO_ACK            (16 B)  sessionId u32 | serverEpochUnixMs_lo u32 | serverEpochUnixMs_hi u32 |
                             deviceNonce u32 (echoed)          ← echo proves freshness
TIME_SYNC_REQ         (4 B)  t0 u32                            (device clock, at send)
TIME_SYNC_RESP       (12 B)  t0 u32 (echoed) | t1 u32 | t2 u32 (laptop clock, rx and tx)
SENSOR_FRAME         (42 B)  deviceTimestampMs u32 | flags u8 | weaponId u8 |
                             dominant[6] i16 | offhand[6] i16 | torso[6] i16
RFID_EVENT           (12 B)  deviceTimestampMs u32 | weaponId u8 | reserved u8 | tagUid[6]
HEARTBEAT            (12 B)  deviceTimestampMs u32 | vbatBelt_mV u16 | vbatDom_mV u16 |
                             vbatOff_mV u16 | statusFlags u16
ACK                   (2 B)  ackedSeq u16
COMBAT_FEEDBACK       (6 B)  feedbackCode u8 | intensityPct u8 | durationMs u16 | repeatCount u16
```

### 3.1 `SENSOR_FRAME.flags` bit layout

| Bit | Name | Meaning |
|---|---|---|
| 0 | `DOM_STALE` | dominant wrist's ESP-NOW frame did not arrive this interval; value is the last-known repeat |
| 1 | `OFF_STALE` | off-hand wrist's ESP-NOW frame did not arrive this interval; value is the last-known repeat |
| 2 | `MOTION_ACTIVE` | motion detected above the idle threshold |
| 3 | `LOW_BATTERY` | any of belt/dominant/offhand below the low-battery threshold |
| 4 | `CALIBRATING` | device is still in its calibration routine; values may be unreliable |
| 5–7 | reserved | must be `0` on send, ignored on receive |

`DOM_STALE`/`OFF_STALE` mean the windower must not feed a stalled signal to the classifier as if it were
fresh — see the relay's `windowing` stage in the comms plan §7.

### 3.2 IMU fixed-point scaling **[config: `shared/config/sensors.yaml`]**

Matches MPU-6050 full-scale settings:

- Accelerometer: `1 LSB = 1/2048 g` (full scale ±16 g)
- Gyroscope: `1 LSB = 1/16.4 °/s` (full scale ±2000 °/s)

The relay converts fixed-point to float only when building `SENSOR_WINDOW`; the wire always carries
fixed-point `int16`.

---

## 4. Sequence numbers and replay

`SEQ` is 16-bit on the wire; both sides maintain a **32-bit extended sequence** (`extSeq`) internally.

On receive, detect wrap with a signed 16-bit delta:

```
delta = int16(new_seq - last_seq)
```

- `delta > 0` (including the wrap case, `new_seq < last_seq`): advance `extSeq`; if `new_seq < last_seq`,
  also increment the high half.
- `delta <= 0`: this is a reorder or replay, not an advance.

Maintain a **64-entry sliding replay window** per direction. Reject any packet whose `extSeq` is at or
below the window floor, or already seen within the window. Log rejections — a sudden burst means either
a bug or an attack, and the log is what lets you tell which during the demo.

---

## 5. Encryption

**AES-128-GCM**, pre-shared 16-byte key **[config]**, loaded from the `CG_PSK_HEX` environment variable
or a file kept outside the repo. Never commit the key. `comms/scripts/gen_psk.py` generates one and
prints the hex for pasting into firmware config.

- **Plaintext:** the payload struct from §3, exactly as laid out.
- **AAD (additional authenticated data):** the 8-byte plaintext header, exactly as it appears on the
  wire (`MAGIC`‖`VERSION`‖`TYPE`‖`SEQ`‖`PLAYER_ID`‖`LEN`). This authenticates `TYPE`/`SEQ`/`LEN`/
  `PLAYER_ID` — an attacker cannot renumber or retype a packet without the tag failing to verify.
- **Nonce (12 bytes), derived, never transmitted on the wire:**

  ```
  nonce = sessionId(u32) ‖ playerId(u8) ‖ direction(u8) ‖ extSeq(u32) ‖ 0x0000
  ```

  where `direction` is `0` for device→laptop and `1` for laptop→device. Because `extSeq` never repeats
  within a `(sessionId, playerId, direction)` tuple, the nonce never repeats — the load-bearing invariant
  for GCM safety.
- **Wire payload:** `ciphertext ‖ tag[16]`, so `LEN = plaintext_len + 16`.

### 5.1 Handshake packets — nonce derivation exception

`HELLO` and `HELLO_ACK` have no session yet, so they cannot use `sessionId`. They must **not** fall back
to `sessionId = 0`: that would derive the identical nonce `(0, playerId, 0, 0, 0)` for every `HELLO` ever
sent — across both devices while `playerId` is still `0`, and across reboots of a single device.
Repeating a `(key, nonce)` pair under AES-GCM leaks the XOR of the plaintexts and enables forgery.

For these two packet types only:

- `PAYLOAD = deviceNonce(u32, **cleartext**) ‖ ciphertext ‖ tag[16]`, so `LEN = 4 + plaintext_len + 16`.
- **AAD** = the 8-byte header ‖ the 4 cleartext `deviceNonce` bytes.
- **Nonce** = `deviceNonce(u32) ‖ playerId(u8) ‖ direction(u8) ‖ extSeq(u32) ‖ 0x0000` — i.e.
  `deviceNonce` occupies the `sessionId` slot. `HELLO_ACK` reuses the same `deviceNonce` with
  `direction = 1`, so the pair never collides.
- `deviceNonce` **must** come from a hardware entropy source (`esp_random()` on the ESP32), never an
  uptime counter or a boot-seeded PRNG — two devices powered on together would otherwise draw the same
  value.
- The laptop's `sessionId` must likewise be random, not a counter.

Replay protection layers on top and is unchanged: the laptop rejects a `HELLO` bearing a `deviceNonce`
seen in the last 60 s **[config]**, and the device rejects a `HELLO_ACK` whose echoed `deviceNonce` does
not match what it just sent.

> **CRC16 + GCM.** The GCM tag already provides integrity, so the CRC is strictly redundant
> cryptographically. Keep it anyway — it is specified in the report, costs almost nothing, and lets the
> receiver discard a corrupted frame before spending a decrypt on it, which matters on the ESP32 side.
> Verify CRC first, then decrypt.

---

## 6. Fragmentation

Negotiate the BLE ATT MTU up at connection time; target ≥ 247 bytes, giving ~244 usable bytes. The
largest payload, `SENSOR_FRAME` at 42 bytes plaintext (68 bytes on the wire including tag/header/CRC), is
well under this — fragmentation should never trigger in practice.

Implement it anyway, because "should never" is not "cannot": reserve the bit and treat a received set
`MORE_FRAGMENTS` as a hard error in Week 5 — log it loudly and drop the packet. Do **not** implement
reassembly until MTU negotiation is observed to fail on real hardware.

If reassembly is ever needed: all fragments carry the same `TYPE` (bit 7 set on every fragment but the
last) and consecutive `SEQ`; fragment the **plaintext** into chunks, encrypt each chunk independently
under its own `extSeq`-derived nonce, and reassemble plaintexts, so every fragment is independently
authenticated. Abandon a partial reassembly after 200 ms **[config]**.

---

## 7. CRC-16/CCITT-FALSE

- Polynomial: `0x1021`
- Initial value: `0xFFFF`
- No input or output reflection, no final XOR.
- Computed over bytes `[0, 8+LEN)` — the full header plus the payload (ciphertext ‖ tag), *not*
  including the CRC field itself.

A reference bit-by-bit implementation (matches `cg_crc16` in `cg_protocol.h`):

```
uint16_t crc = 0xFFFF;
for each byte b in message:
    crc ^= (uint16_t)b << 8;
    for i in 0..7:
        if crc & 0x8000: crc = (crc << 1) ^ 0x1021;
        else:            crc = crc << 1;
return crc;
```

---

## 8. Golden vectors

`shared/protocol/golden_vectors.json` (added when the crypto/codec implementation lands, not part of
this contract-freeze pass) fixes, for each packet type: a PSK, `sessionId`, `seq`, and plaintext payload,
plus the exact expected wire bytes in hex. `comms/tests/test_golden_vectors.py` and
`firmware/test/test_golden_vectors.cpp` both assert against the same file, so the Python and C
implementations are provably byte-for-byte compatible before a FireBeetle and a laptop are ever in the
same room.

---

## 9. Handshake and time sync sequence

```
device → laptop   HELLO            {mac, playerId, protoVer, fwVer, deviceNonce}
laptop → device   HELLO_ACK        {sessionId, serverEpochUnixMs, deviceNonce echo}
device → laptop   TIME_SYNC_REQ    {t0}                       ×8, 100 ms spacing
laptop → device   TIME_SYNC_RESP   {t0, t1, t2}
                  ── streaming begins ──
device → laptop   SENSOR_FRAME @ 50 Hz, RFID_EVENT on change, HEARTBEAT @ 1 Hz
laptop → device   COMBAT_FEEDBACK on event, HEARTBEAT @ 1 Hz
```

Reject `HELLO` if `protocolVersion != VERSION` — respond with a `HELLO_ACK` carrying `sessionId = 0`
(refusal) and log loudly. A silent version mismatch is the classic integration-day time sink.

Offset estimation is the standard NTP four-timestamp form, with `t3` recorded by the device on arrival:

```
offset = ((t1 - t0) + (t2 - t3)) / 2
rtt    =  (t3 - t0) - (t2 - t1)
```

See `docs/COMMS_IMPLEMENTATION_PLAN.md` §5 for the full sampling, outlier-rejection, and drift-tracking
procedure — that is relay behavior, not wire format, so it is not repeated here.
