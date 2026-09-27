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
| `SENSOR_FRAME`, `HEARTBEAT` | best-effort | Dropped capture frames can cause too_few/span suppression; never fill with unrelated frames |
| `RFID_EVENT`, `COMBAT_FEEDBACK` | ACK + identical-ciphertext retry | One application effect; exact duplicates get another ACK |
| `HELLO`, `HELLO_ACK`, `TIME_SYNC_*` | Bounded request/response | Failure restarts handshake with a fresh device nonce and BLE session; no generic ACK exchange |
| `ACK` | never acked | obviously |

---

## 3. Payloads (plaintext, before encryption)

Device timestamps are uint32 milliseconds since device boot. TIME_SYNC_RESP t1/t2 carry the
low 32 bits of laptop monotonic milliseconds; HELLO_ACK's Unix epoch is informational in this demo.
Laptop HEARTBEAT's timestamp is low-32-bit Unix milliseconds and is used only for liveness here.
These fields are not interchangeable clock domains. Device-clock wrap/long-term drift correction
remains a documented limitation; the demo uses initial synchronization only.

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

Maintain a **64-entry sliding replay window** per direction for ordinary traffic; commit it only
after authentication. Each fresh session resets both TX and RX state. Failed stack sends burn
their encoded sequence/nonce; gaps are expected. Exact retransmission reuses ciphertext, not a
fresh encryption under the same nonce.

Streaming reliable types additionally use an independent extended-sequence watermark (one
application effect per increasing reliable sequence). This admits the first delivery after more
than 64 sensor packets. A bounded 16-entry/20-second cache of authenticated ciphertexts permits
duplicate re-ACKs even after the ordinary window moves on; duplicates never repeat effects.
Older unseen reliable packets below the reliable watermark are superseded/dropped; evicted or
expired duplicates cannot apply again. ACKs are never cached as reliable and never ACKed.

RFID retries: firmware assigns one seq, tries at 0, 0.5, 1.5, 3.5, 7.5 seconds, abandons at
15.5 seconds without ACK, and logs exhaustion. A new selection supersedes the pending selection.
Relay feedback uses configured ReliabilitySender timeout/backoff/jitter, max five attempts, with
identical frame bytes. Demo relay YAML uses 200 ms base; firmware RFID uses 500 ms.
The 20-second duplicate cache covers these retry budgets. Neither sender pauses sensor streaming.

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
seen in the current Session object within its 60 s cache, and the device rejects a `HELLO_ACK` whose echoed `deviceNonce` does
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

The protocol can express the four-timestamp NTP estimate, but current demo firmware only checks
response type, nonce/session, and echoed t0. The relay sees t0/t1/t2 only and seeds its offset from
t1-t0; t2-t1 is processing time, not RTT. No steady-state resync timer is currently wired up.
Sample spread is logged as `timesync_seeded.sample_spread_ms`; it is not an accuracy bound.
See [PRE_DEMO_ROBUSTNESS.md](../../docs/PRE_DEMO_ROBUSTNESS.md) for clock limitations and physical tests.


## Assessment extension (reviewed firmware revision 3)

Base wire version remains 1. Deploy the assessment firmware and relay changes together: the earlier
assessment prototype used different payload lengths for types 0x40..0x43 and is not compatible.
Normal packet layouts are unchanged. All fields below use the existing little-endian codec,
AES-GCM/header AAD, CRC and per-direction sequence allocation.

| Type | Code | Direction | Plaintext / complete frame bytes |
|---|---|---|---|
| ASSESS_ECHO_REQ | 0x40 | laptop -> device | 48 / 74 |
| ASSESS_ECHO_RESP | 0x41 | device -> laptop | 50 / 76 |
| ASSESS_FILE_CHUNK | 0x42 | laptop -> device | 42 / 68 |
| ASSESS_FILE_ACK | 0x43 | device -> laptop | 12 / 38 |
| ASSESS_FILE_READ | 0x44 | laptop -> device | 6 / 32 |
| ASSESS_FILE_DATA | 0x45 | device -> laptop | 42 / 68 |
| ASSESS_WEAPON_REQ | 0x46 | laptop -> device | 4 / 30 |
| ASSESS_WEAPON_RESP | 0x47 | device -> laptop | 9 / 35 |

```text
ECHO_REQ:   trace_id u32 | keycode u8 | payload_kind u8 | SENSOR_BODY[42]
ECHO_RESP:  trace_id u32 | keycode u8 | payload_kind u8 | transform_id u8 | reserved u8
            | SENSOR_BODY[42]
FILE_CHUNK: transfer_id u32 | chunk_index u16 | total_chunks u16 | chunk_len u8
            | reserved u8 | data[32]
FILE_ACK:   transfer_id u32 | chunk_index u16 | running_crc u16 | received_bytes u32
FILE_READ:  transfer_id u32 | chunk_index u16
WEAPON_REQ: trace_id u32
WEAPON_RESP: trace_id u32 | device_timestamp_ms u32 | weapon_id u8
FILE_DATA:  transfer_id u32 | chunk_index u16 | total_bytes u16 | chunk_len u8
            | reserved u8 | data[32]
```

SENSOR_BODY has the exact 42-byte SENSOR_FRAME layout. In ECHO_REQ the timestamp slot is
`sent_ms`, the relay's low-32-bit monotonic milliseconds. In ECHO_RESP it is device milliseconds.
Do not subtract these unsynchronized values to estimate RTT; relay RTT uses its local clock.
The dominant acceleration triplet is named `value_a/value_b/value_c`; the remaining axes are
`dominant_gyro[3]`, `offhand[6]`, `torso[6]`. Flags are zero and weapon is 1..3 for this demo.
For keyboard b/c/d, firmware revision 3 returns every raw axis unchanged (IDENTITY=0).
The key implies the requested transform; the response carries the explicit transform ID.
Revision 2 firmware has no identity selector and must be reflashed for this keyboard path.

For other legacy low-level keys, transform ID is `(keycode % 3) + 1`:
1 negates the triplet, 2 adds keycode to each value,
3 returns `(2*c, trunc_toward_zero(a/2), -b)`. All outputs saturate to int16.
Trace/transfer IDs must be nonzero. Reserved bytes and unused trailing data bytes must be zero.
Files contain 1..4096 bytes; chunks are indexed from zero, nonfinal chunks hold 32 bytes,
and the final chunk holds 1..32. Running CRC is CCITT-FALSE, initial value 0xFFFF.

ECHO_REQ, WEAPON_REQ, FILE_CHUNK and FILE_READ use transport ACK plus bounded identical-ciphertext retry.
Replies themselves are not ACKed. The device caches its last assessment response and re-emits
it under a fresh outgoing sequence when the exact request repeats, without applying it twice.
The relay also retries the original request if its ACK arrived but its application reply did not.
One assessment operation is active per board; the existing reliable feedback lane is independent.
Malformed requests do not commit replay/application state. A response refused by the BLE stack
also leaves that operation uncommitted. This does not guarantee radio delivery; retries remain bounded.

FILE_ACK reports device upload state, not an echo of the source hash. The firmware retains the
accepted file in a 4096-byte RAM buffer. FILE_READ is permitted only for a complete matching transfer.
FILE_DATA contains bytes copied from that buffer. Relay final success requires byte equality and
SHA-256 equality after all readback chunks arrive, and the returned bytes are saved separately.
Reset clears the buffer; there is no persistent storage or cross-session resume.

WEAPON_REQ invokes the same board weapon-cycle routine as GPIO4 (1 SWORD, 2 SPEAR, 3 MACE).
The response must be accepted by the BLE stack before the selection commits; an exact request
retry returns the cached response without cycling again. Selection also uses the existing reliable
RFID_EVENT announcement. See [deterministic keyboard demo](../../docs/DETERMINISTIC_KEYBOARD_DEMO.md)
for fixed mappings, live input editing and the optional deterministic classifier mode.

See [the reviewed operator runbook](../../docs/COMMS_ASSESSMENT_REVIEW_AND_RUNBOOK.md) for
fault injection, throughput evidence, physical retests and limitations of the one-sample dummy AI path.
