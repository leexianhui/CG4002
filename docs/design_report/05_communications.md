# Section 4: Communications

## 4.1 FireBeetle Setup and Configuration

Each belt-mounted FireBeetle ESP32 connects to the player's laptop as a BLE peripheral. BLE is the primary connection because it uses less power and avoids shared WiFi congestion. WiFi station mode is used as a fallback. The dominant and non-dominant wrist units use separate ESP32-C3 boards with individual IMUs. The dominant wrist also includes an RFID reader. Both units send encrypted sensor data to the FireBeetle through ESP-NOW [9]. RFID readings are debounced into confirmed weapon IDs before transmission.

On startup, the FireBeetle:

1. Initializes the torso IMU and ESP-NOW receiver.
2. Advertises a BLE service containing the player ID.
3. Completes a handshake with the laptop.
4. Synchronizes its clock with the laptop.

The FireBeetle combines the torso and wrist readings into one sensor frame before sending it to the laptop.

> **Fig 4.1 — System Communication Architecture.** PlantUML source: `image_prompts.md`, section "Fig 4.1".

## 4.2 FireBeetle to Laptop Protocol

### Handshake Process

The connection uses the following handshake:

1. FireBeetle to Laptop: `HELLO { deviceId, playerId, firmwareVersion }`
2. Laptop to FireBeetle: `HELLO_ACK { assignedSessionId, serverTimeMs }`
3. FireBeetle to Laptop: `TIME_SYNC_REQ { t0 }`
4. Laptop to FireBeetle: `TIME_SYNC_RESP { t0, t1, t2 }`

The FireBeetle records the response arrival time as `t3`. The clock offset is calculated as:

```
offset = ((t1 - t0) + (t2 - t3)) / 2
```

This offset aligns both players' timestamps with the Game Engine clock. Sensor transmission begins only after `HELLO_ACK` is received. `HEARTBEAT` packets are then exchanged to monitor the connection.

> **Fig 4.2.1 — FireBeetle to Laptop Handshake.** PlantUML source: `image_prompts.md`, section "Fig 4.2.1".

### Packet Types

| Type | Direction | Purpose |
|---|---|---|
| `HELLO` | FireBeetle to Laptop | Identifies the device and starts a session |
| `HELLO_ACK` | Laptop to FireBeetle | Confirms and assigns session |
| `TIME_SYNC_REQ` | FireBeetle to Laptop | Request clock synchronization |
| `TIME_SYNC_RESP` | Laptop to FireBeetle | Returns synchronization timestamps |
| `SENSOR_FRAME` | FireBeetle to Laptop | Sends combined torso and wrist data |
| `RFID_EVENT` | FireBeetle to Laptop | Sends a confirmed weapon ID |
| `HEARTBEAT` | Both | Monitors connection status |
| `ACK` | Both | Acknowledgement of reliable packets |
| `COMBAT_FEEDBACK` | Laptop to FireBeetle | Trigger haptic or LED actuator feedback |

### Packet Format

A fixed binary header is used to minimize communication overhead on the constrained BLE or Wi-Fi link. Each packet follows the format below:

| Field | Size | Description |
|---|---|---|
| `MAGIC` | 1 byte | Start marker |
| `VERSION` | 1 byte | Protocol version |
| `TYPE` | 1 byte | Packet type |
| `SEQ` | 2 bytes | Increasing sequence number |
| `PLAYER_ID` | 1 byte | Player identifier |
| `LEN` | 2 bytes | Payload length |
| `PAYLOAD` | LEN bytes | Packet data |
| `CRC16` | 2 bytes | Checksum |

The sequence number is used to detect duplicate, missing, or out-of-order packets.

> **Fig 4.2.2 — Packet Format.** PlantUML (salt) source: `image_prompts.md`, section "Fig 4.2.2".

## 4.3 Reliability

> **Fig 4.3 — Reliability Mechanisms.** PlantUML source: `image_prompts.md`, section "Fig 4.3".

**Fragmentation.** Packets are kept below the link's supported payload size. BLE uses a negotiated extended ATT MTU [10], while WiFi supports larger packets. Oversized payloads are split into fragments using a `MORE_FRAGMENTS` flag and reassembled by sequence number. Fragmentation should be rare because sensor frames are small.

**Selective Reliability.** `SENSOR_FRAME` packets use best-effort delivery because occasional losses can be handled by the classification window. State-changing packets, including `HELLO` and `RFID_EVENT`, require acknowledgements. Unacknowledged packets are retransmitted with a limited retry count and exponential backoff.

**Connection Drop Detection.** The FireBeetle and laptop exchange `HEARTBEAT` packets regularly. If no heartbeat or data packet is received within the timeout period, the link is marked as stale and combat processing for that player is paused. Both devices then attempt automatic reconnection through BLE or WiFi.

**Backpressure.** The laptop uses a bounded receive queue. If processing falls behind, older sensor frames are discarded in favour of newer data. This prevents increasing classifier latency.

## 4.4 Secure Communication

> **Fig 4.4 — Secure Communication Architecture.** PlantUML source: `image_prompts.md`, section "Fig 4.4".

**Message Format.** Each packet contains a plaintext header, encrypted payload, and authentication tag. The header includes the packet type, sequence number, and payload length.

**FireBeetle to Laptop Security.** FireBeetle-to-laptop traffic uses AES-128-GCM, which provides encryption and integrity protection. A pre-shared key is configured during setup because the devices are manually paired before a match.

**Wrist Units to FireBeetle.** Each wrist unit encrypts its ESP-NOW data using AES-128-CCM. Encryption is handled by the wearable hardware before the FireBeetle receives the data.

**Laptop/Ultra96 to Game Engine to Unity Visualizers.** The Central Game Engine runs on the Ultra96 itself, alongside the classifiers, so both the classifier link and the Game Engine connection are laptop/phone-to-Ultra96 links rather than laptop-to-laptop links. Unity Visualizer connections to the Game Engine use TLS-secured WebSockets, authenticated with a shared bearer token, consistent with Section 4.6. The classifier link (laptop to Ultra96, `SENSOR_WINDOW`/`ACTION_RESULT`) is plain line-delimited JSON over TCP at match time; see the accepted-risk note at the end of Section 4.5 for why this is judged acceptable rather than upgraded to a second TLS connection.

**Network Tunnelling (development only).** During development, the Ultra96 sits on the NUS SoC Research Subnet, and is reached from a laptop through the NUS VPN and an SSH tunnel, as described in Section 4.5. This tunnel is a development and remote-access convenience, not part of the live match path: on match day, the Ultra96 is instead deployed as a whitelisted device directly on the venue WiFi (Section 2.1), so it is reachable by every other device without a tunnel.

## 4.5 Laptop to Ultra96 Communication

**Match-time path.** At the venue, the Ultra96 is provisioned as a whitelisted device on the match WiFi network. Because the venue's WiFi enforces client isolation between personal devices, but exempts whitelisted devices, both players' laptops and both AR visualizer phones can each open a direct connection to the Ultra96, while none of them can connect directly to one another. The laptop's Ultra96 client connects over this WiFi straight to the Ultra96's classifier service, and separately to the Game Engine's WebSocket endpoint also hosted on the Ultra96 (Section 4.6). This assumes the Ultra96 can in fact be registered as a whitelisted device on the venue's network for the demonstration; if that is not possible, the development path below would need to be used at match time instead, which is noted here as a risk rather than assumed away.

**Development and remote-access path.** Outside of a live match, the Ultra96 is normally fixed in Makers@SoC on the NUS SoC Research Subnet, while student devices use the WiFi or VPN subnet. Direct inbound connections are blocked, but SSH access through port 22 is available through the NUS VPN and jump host. A developer creates a local SSH tunnel to reach it:

```
ssh -J <SoC-UNIX-ID>@stfjump.comp.nus.edu.sg -N -L localhost:8888:localhost:8888 xilinx@<host-id>
```

The Communications process then connects to `localhost:8888`, and SSH forwards the traffic to the classifier service on the Ultra96. This is used for coding, deployment, logging, and diagnostics when physical lab access is unavailable — not for gameplay traffic during a match, which uses the direct path above instead.

**Accepted risk on the classifier link.** With the tunnel out of the match-time path, the laptop-to-Ultra96 classifier connection is no longer wrapped in SSH's encryption. Given that this traffic stays entirely within the isolated venue WiFi and carries only sensor windows and classification results rather than credentials, this is treated as an acceptable risk for the demonstration rather than a gap requiring a second TLS-secured socket alongside the one already used for the Game Engine connection (Section 4.4).

Communication uses line-delimited JSON at approximately 10 Hz. Each `SENSOR_WINDOW` contains calibrated dominant-wrist and off-hand samples. The dominant wrist is used for attack classification, while the off-hand wrist is used for guard classification.

Example request:

```json
{ "type": "SENSOR_WINDOW", "playerId": 1, "weapon": "SWORD", "timestamp": 12345678,
  "dominantWristSamples": [[0, 0, 0, 0, 0, 0]], "offHandSamples": [[0, 0, 0, 0, 0, 0]] }
```

The Ultra96 runs both classifiers and returns one result:

```json
{ "type": "ACTION_RESULT", "playerId": 1, "weapon": "SWORD",
  "action": "HORIZONTAL_SWING", "actionConfidence": 0.94,
  "guardState": "IDLE", "guardConfidence": 0.88, "timestamp": 12345680 }
```

## 4.6 Ultra96 to Visualizer Communication

Ultra96 results are normalized and handed to the Central Game Engine process running on the same Ultra96 board, so this handoff never crosses the network. The Game Engine processes both players' events and sends updates to both Unity Visualizers through secure WebSocket connections, and receives each laptop's relayed `ACTION`/`WEAPON_CHANGED` events over the same kind of connection from the other side.

The main message types are:

- `ACTION`
- `POSITION`
- `WEAPON_CHANGED`
- `COMBAT_EVENT`

The Game Engine sends actions, weapon changes, opponent position, and combat events to both Visualizers. Each Visualizer sends phone-based position updates to the Game Engine.

Example position message:

```json
{ "type": "POSITION", "playerId": 2, "timestamp": 12345690, "x": 1.2, "y": 0.4, "heading": 87.5 }
```

Each application message includes a schema version, timestamp, and player ID. WebSocket is used because it allows the Game Engine to push updates immediately without polling [11].

> **Fig 4.6 — End-to-End Data Pipeline.** PlantUML source: `image_prompts.md`, section "Fig 4.6".

## 4.7 Central Coordination, Parallelism and Synchronization

The Communications module uses a single asynchronous event loop, such as Python `asyncio`. This reduces lock contention and simplifies shared-state management. The classifiers and the Game Engine are separate processes colocated on the same physical Ultra96 board (Section 2.1); each still runs its own event loop, and the coordination described below is internal to the Game Engine process.

**Inbound Stream Processing.** Separate asynchronous tasks handle:

1. Player 1 sensor data.
2. Player 2 sensor data.
3. Player 1 AI results.
4. Player 2 AI results.
5. Player 1 position updates.
6. Player 2 position updates.

Each task validates and normalizes messages before placing them in an internal queue.

**Central Game State.** A single Game State task consumes queued events and maintains each player's weapon, action, position, guard state, and health. Only this task can modify the shared state, preventing race conditions.

**Combat Resolution.** When an `ACTION` event arrives, the Game Engine first checks whether the detected action is currently available or on cooldown. If the action is on cooldown, it is ignored. Otherwise, the Game Engine compares it with the defender's latest position and guard state, applies the combat rules, generates a `COMBAT_EVENT`, and starts the corresponding cooldown timer for that action.

**Event Broadcasting.** A broadcast task sends authoritative updates to both Unity Visualizers concurrently, keeping their game states synchronized.

**Synchronization Between Players.** FireBeetle timestamps are corrected using the calculated clock offset. Combat decisions therefore use event time instead of message arrival order. For example, a parry may require the attack and guard actions to occur within a configured 150 ms window.

**Mock-Data Support.** A mock-data generator can place synthetic `ACTION`, `POSITION`, and `WEAPON_CHANGED` events into the same queues used by hardware data. This allows the Game Engine, combat rules, and Visualizers to be tested without physical devices.

> **Fig 4.7 — Central Coordination Architecture.** PlantUML source: `image_prompts.md`, section "Fig 4.7".
