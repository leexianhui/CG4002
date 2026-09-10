# Figure Diagrams

All figures below were originally AI-image-generation prompts. Since every one of them is a structural diagram (deployment, sequence, packet layout, or process flow), they have been converted to PlantUML instead, so the diagrams are exact and easy to keep in sync with the text, rather than approximated by an image model.

**This file is the single point of truth for every diagram in the report.** The previously checked-in
hand-built `figures/*.svg` files have been deleted — the Google Doc is populated by rendering the
PlantUML blocks below and pasting the results in, so keeping a second copy on disk only risked the two
drifting apart.

To render any block: paste it into https://www.plantuml.com/plantuml/uml/ (or the PlantUML VS Code extension, or `plantuml.jar` locally) and export as SVG/PNG. Colors are consistent across all diagrams (blue `#2c5f8a`, purple `#6a4c93`, light fills `#eaf2fb`/`#f1ecf9`).

Three figures in the report are *not* PlantUML and are not included here, because they are illustrations
rather than structural diagrams:

- **Fig 2.2** — Intended Wearable Configuration (labelled front/back sketch: FireBeetle, both wrist IMUs, torso IMU, RFID reader, battery placement).
- **Fig 6.1** — Gameplay display layout (UI mockup).
- **Fig 6.3** — Headset optics and enclosure requirements (camera opening + septum sketch).

Section numbering below follows the current report: Communications is Section 4, AI is Section 5,
Software Visualizer is Section 6, Project Management Plan is Section 7.

## Fig 2.1 — Overall System Deployment Diagram (System Architecture section)

Placement: Section 2.1 "High-Level Architecture", at the Fig 2.1 placeholder. Not part of Section 4.

```plantuml
@startuml fig_2_1_deployment_diagram
skinparam backgroundColor white
skinparam defaultTextAlignment center
skinparam node {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}
skinparam artifact {
  BackgroundColor #ffffff
  BorderColor #2c5f8a
}
skinparam rectangle {
  BackgroundColor #f1ecf9
  BorderColor #6a4c93
}

title Fig 2.1 - Overall System Deployment Diagram

package "Player 1" {
  node "Dominant Wrist Unit - P1" <<device>> as WristD1 {
    artifact "IMU" as WristD1Imu
    artifact "RFID reader" as WristD1Rfid
  }
  node "Non-Dominant Wrist Unit - P1" <<device>> as WristN1 {
    artifact "IMU" as WristN1Imu
  }
  node "FireBeetle ESP32 - P1\n(belt unit)" <<device>> as FbP1 {
    artifact "Torso IMU (I2C, on-board)" as FbImuP1
    artifact "Sensor acquisition + framing" as FbArtP1
  }
  node "Laptop - P1" <<device>> as LapP1 {
    artifact "Comms Relay Process" as RelayP1
  }
}

package "Player 2" {
  node "Dominant Wrist Unit - P2" <<device>> as WristD2 {
    artifact "IMU" as WristD2Imu
    artifact "RFID reader" as WristD2Rfid
  }
  node "Non-Dominant Wrist Unit - P2" <<device>> as WristN2 {
    artifact "IMU" as WristN2Imu
  }
  node "FireBeetle ESP32 - P2\n(belt unit)" <<device>> as FbP2 {
    artifact "Torso IMU (I2C, on-board)" as FbImuP2
    artifact "Sensor acquisition + framing" as FbArtP2
  }
  node "Laptop - P2" <<device>> as LapP2 {
    artifact "Comms Relay Process" as RelayP2
  }
}

node "Ultra96 FPGA\n(shared, whitelisted on venue WiFi)" <<device>> as U96 {
  artifact "Weapon Action Classifier" as ClassU96
  artifact "Guard Classifier" as GuardU96
  artifact "Central Game Engine" as GameEngine
}

node "Phone / AR Headset - P1" <<device>> as PhoneP1 {
  artifact "Unity AR Visualizer" as VisArtP1
}
node "Phone / AR Headset - P2" <<device>> as PhoneP2 {
  artifact "Unity AR Visualizer" as VisArtP2
}

WristD1 --> FbP1 : ESP-NOW\n(AES-128-CCM)
WristN1 --> FbP1 : ESP-NOW\n(AES-128-CCM)
FbP1 --> WristD1 : ESP-NOW\n(COMBAT_FEEDBACK relay,\ndrives haptic + LED locally)
FbP1 --> LapP1 : BLE (primary) /\nWiFi (fallback)

WristD2 --> FbP2 : ESP-NOW\n(AES-128-CCM)
WristN2 --> FbP2 : ESP-NOW\n(AES-128-CCM)
FbP2 --> WristD2 : ESP-NOW\n(COMBAT_FEEDBACK relay,\ndrives haptic + LED locally)
FbP2 --> LapP2 : BLE (primary) /\nWiFi (fallback)

RelayP1 <--> U96 : venue WiFi\n(SENSOR_WINDOW down,\nACTION_RESULT back;\nACTION/WEAPON_CHANGED\nover WSS to GameEngine)
RelayP2 <--> U96 : venue WiFi\n(SENSOR_WINDOW down,\nACTION_RESULT back;\nACTION/WEAPON_CHANGED\nover WSS to GameEngine)

GameEngine --> PhoneP1 : WSS: ACTION,\nWEAPON_CHANGED,\nCOMBAT_EVENT
GameEngine --> PhoneP2 : WSS: ACTION,\nWEAPON_CHANGED,\nCOMBAT_EVENT
PhoneP1 --> GameEngine : WSS: POSITION\n(AR Foundation tracking)
PhoneP2 --> GameEngine : WSS: POSITION\n(AR Foundation tracking)

note bottom of U96
  Runs as the shared hub for both players.
  The venue's school WiFi blocks personal
  devices (laptops, phones) from connecting
  directly to each other, but exempts
  whitelisted devices. The Ultra96 is
  whitelisted, so every laptop and phone
  connects only to it, never to one another.
  During development the Ultra96 instead
  sits on the NUS SoC Research Subnet,
  reached over VPN + SSH tunnel (Section 4.5);
  that path is not used during a live match.
end note

@enduml
```

## Fig 3.1 — Continuous Sense-Process-Communicate-React Loop

Placement: Section 3.1, at the Fig 3.1 placeholder immediately after the three-unit bullet list.

```plantuml
@startuml fig_3_1_sense_process_communicate_react
skinparam backgroundColor white
skinparam activity {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}
skinparam partition {
  BorderColor #6a4c93
}

title Fig 3.1 - Continuous Sense-Process-Communicate-React Loop

|Wrist Units (ESP32-C3)|
start
repeat
  :SENSE - sample wrist IMU at 200 Hz (I2C);
  note right
    Dominant wrist also polls
    MFRC522 RFID at ~5 Hz
  end note
  :PROCESS - offset calibration, median filter,
  low-pass filter, accel/gyro fusion,
  motion-energy gating;
  :COMMUNICATE - AES-128-CCM encrypt,
  ESP-NOW TX at 50 Hz;

  |Belt Unit (FireBeetle ESP32)|
  :SENSE - sample torso IMU at 100 Hz (I2C);
  :PROCESS - ESP-NOW RX + decrypt both wrists,
  relative-frame transform against torso,
  merge into one SENSOR_FRAME;
  :COMMUNICATE - AES-128-GCM encrypt,
  BLE TX to laptop at 50 Hz (fixed cadence);

  :REACT - receive COMBAT_FEEDBACK from laptop,
  relay event code to dominant wrist over ESP-NOW;

  |Dominant Wrist Unit (ESP32-C3)|
  :REACT - event-to-pattern state machine drives
  haptic PWM pattern and WS2812 LED colour/blink;
repeat while (match running?) is (yes)
->no;
stop

@enduml
```

## Fig 4.1 — System Communication Architecture

Placement: Section 4.1 "FireBeetle Setup and Configuration", at the Fig 4.1 placeholder at the end of that subsection.

```plantuml
@startuml fig_4_1_system_communication_architecture
skinparam backgroundColor white
skinparam componentStyle rectangle
skinparam component {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}

title Fig 4.1 - System Communication Architecture

component "Dominant Wrist Unit\n(IMU + RFID reader)" as WristD
component "Non-Dominant Wrist Unit\n(IMU)" as WristN
component "FireBeetle ESP32\n(belt unit, torso IMU via I2C)" as FireBeetle
component "Laptop\n(Comms Relay)" as Laptop
component "Ultra96 (shared)\nClassifiers + Central Game Engine" as Ultra96
component "Unity Visualizer\nPlayer 1" as VisP1
component "Unity Visualizer\nPlayer 2" as VisP2

WristD --> FireBeetle : ESP-NOW (AES-128-CCM)
WristN --> FireBeetle : ESP-NOW (AES-128-CCM)
FireBeetle --> WristD : ESP-NOW (COMBAT_FEEDBACK relay)\ndrives haptic motor + status LED locally
FireBeetle --> Laptop : BLE (primary) / WiFi (fallback)\nClock sync / timestamp alignment
Laptop <--> Ultra96 : venue WiFi, bidirectional\n(SENSOR_WINDOW/ACTION_RESULT to classifiers;\nACTION/WEAPON_CHANGED to Game Engine over WSS)
Ultra96 --> VisP1 : WSS: ACTION, WEAPON_CHANGED,\nCOMBAT_EVENT
Ultra96 --> VisP2 : WSS: ACTION, WEAPON_CHANGED,\nCOMBAT_EVENT
VisP1 --> Ultra96 : WSS: POSITION\n(AR Foundation tracking)
VisP2 --> Ultra96 : WSS: POSITION\n(AR Foundation tracking)

note right of Ultra96
  Both players' laptops and phones connect
  only to this shared Ultra96, never to each
  other - the venue WiFi blocks personal-device-
  to-personal-device traffic but exempts the
  whitelisted Ultra96. During development the
  Ultra96 is reached over VPN + SSH tunnel
  instead (Section 4.5); that path is not used
  at match time.
end note

@enduml
```

## Fig 4.2.1 — FireBeetle to Laptop Handshake

Placement: Section 4.2, right after the "Handshake Process" paragraph, before the "Packet Types" table.

```plantuml
@startuml fig_4_2_1_handshake
skinparam backgroundColor white
skinparam sequence {
  ArrowColor #2c5f8a
  LifeLineBorderColor #2c5f8a
  ParticipantBorderColor #2c5f8a
  ParticipantBackgroundColor #eaf2fb
}

title Fig 4.2.1 - FireBeetle to Laptop Handshake

participant "FireBeetle" as FB
participant "Laptop" as L

FB -> L : HELLO { deviceId, playerId, firmwareVersion }
L -> FB : HELLO_ACK { assignedSessionId, serverTimeMs }
FB -> L : TIME_SYNC_REQ { t0 }
L -> FB : TIME_SYNC_RESP { t0, t1, t2 }
note right of FB
  FireBeetle records its own local
  receive time t3 on arrival.
  offset = ((t1-t0) + (t2-t3)) / 2
  RTT = (t3-t0) - (t2-t1)
end note

== streaming begins only after HELLO_ACK ==
FB -> L : SENSOR_FRAME (repeats every sampling interval)
FB -> L : HEARTBEAT (periodic, keeps link alive, detects drops)

@enduml
```

## Fig 4.2.2 — Packet Format

Placement: Section 4.2, right after the Packet Format table (after "The sequence number is used to detect duplicate, missing, or out-of-order packets.").

```plantuml
@startsalt
title Fig 4.2.2 - Packet Format (fixed binary header)
{#
  MAGIC\n1B | VERSION\n1B | TYPE\n1B | SEQ\n2B | PLAYER_ID\n1B | LEN\n2B | PAYLOAD\nLEN bytes | CRC16\n2B
}
@endsalt
```

## Fig 4.3 — Reliability Mechanisms

Placement: Section 4.3, directly under the "4.3 Reliability" heading, at the Fig 4.3 placeholder.

```plantuml
@startuml fig_4_3_reliability_mechanisms
skinparam backgroundColor white
skinparam rectangle {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}

title Fig 4.3 - Reliability Mechanisms

rectangle "Fragmentation" {
  rectangle "Payload exceeds MTU\n(about 180B BLE / 1400B WiFi)" as F1
  rectangle "Split with MORE_FRAGMENTS flag,\nreassembled by SEQ on laptop side" as F2
  F1 --> F2
}

rectangle "Selective Reliability" {
  rectangle "SENSOR_FRAME:\nbest-effort, unacknowledged" as S1
  rectangle "RFID_EVENT / HELLO:\nACK + retransmit, bounded retries,\nexponential backoff" as S2
}

rectangle "Connection Monitoring" {
  rectangle "HEARTBEAT exchanged\nFireBeetle <-> Laptop" as H1
  rectangle "Timeout (e.g. 3x interval):\npause combat resolution for that\nplayer, attempt auto-reconnect" as H2
  H1 --> H2
}

rectangle "Backpressure" {
  rectangle "Bounded laptop-side\nreceive queue" as B1
  rectangle "Pipeline falls behind:\ndrop oldest frames, keep newest\n(bounds latency to classifier)" as B2
  B1 --> B2
}

@enduml
```

## Fig 4.4 — Secure Communication Architecture

Placement: Section 4.4, directly under the "4.4 Secure Communication" heading, at the Fig 4.4 placeholder.

```plantuml
@startuml fig_4_4_secure_communication
skinparam backgroundColor white
skinparam component {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}

title Fig 4.4 - Secure Communication Architecture

component "Dominant Wrist Unit\n(IMU + RFID) - P1" as WristD1
component "Non-Dominant Wrist Unit\n(IMU) - P1" as WristN1
component "FireBeetle ESP32 - P1" as FireBeetle1
component "Laptop P1\n(Comms Relay)" as LaptopP1

component "Dominant Wrist Unit\n(IMU + RFID) - P2" as WristD2
component "Non-Dominant Wrist Unit\n(IMU) - P2" as WristN2
component "FireBeetle ESP32 - P2" as FireBeetle2
component "Laptop P2\n(Comms Relay)" as LaptopP2

component "Ultra96 (shared)\nClassifiers" as Ultra96
component "Central Game Engine\n(on the shared Ultra96)" as GameEngine
component "Unity Visualizers\n(P1 and P2)" as Visualizers

WristD1 --> FireBeetle1 : AES-128-CCM (ESP-NOW)
WristN1 --> FireBeetle1 : AES-128-CCM (ESP-NOW)
FireBeetle1 --> WristD1 : AES-128-CCM (ESP-NOW)\nCOMBAT_FEEDBACK relay
FireBeetle1 --> LaptopP1 : AES-128-GCM (BLE / WiFi)
LaptopP1 <--> Ultra96 : venue WiFi, no tunnel at match time\n(plain TCP JSON - accepted risk, Section 4.5)

WristD2 --> FireBeetle2 : AES-128-CCM (ESP-NOW)
WristN2 --> FireBeetle2 : AES-128-CCM (ESP-NOW)
FireBeetle2 --> WristD2 : AES-128-CCM (ESP-NOW)\nCOMBAT_FEEDBACK relay
FireBeetle2 --> LaptopP2 : AES-128-GCM (BLE / WiFi)
LaptopP2 <--> Ultra96 : venue WiFi, no tunnel at match time\n(plain TCP JSON - accepted risk, Section 4.5)

LaptopP1 --> GameEngine : TLS / WSS (venue WiFi)
LaptopP2 --> GameEngine : TLS / WSS (venue WiFi)
GameEngine --> Visualizers : TLS / WSS (venue WiFi)

note bottom
  Payload format at every hop:
  { header (plaintext: type, seq, len),
    ciphertext, auth_tag }
  Ultra96 <-> laptop is the exception: plain
  JSON, no per-hop encryption (Section 4.5).
  A development-only SSH tunnel (Section 4.5)
  replaces this direct link off-venue.
end note

@enduml
```

## Fig 4.6 — End-to-End Data Pipeline

Placement: Section 4.6 "Ultra96 to Visualizer Communication", at the Fig 4.6 placeholder at the end of that subsection.

```plantuml
@startuml fig_4_6_end_to_end_pipeline
skinparam backgroundColor white
skinparam activity {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}

title Fig 4.6 - End-to-End Data Pipeline

start
:Wrist IMUs (dominant + non-dominant) and torso IMU\nsample raw motion; dominant wrist RFID reader\nconfirms the active weapon ID;
:Wrist units encrypt (AES-128-CCM) and send over\nESP-NOW to the belt FireBeetle, which merges all\nthree IMU readings into one combined frame;
:Laptop assembles combined frames into two separate\nsensor windows: one from the dominant wrist,\none from the off-hand wrist;
:Ultra96 runs two classifiers on the same window pair:\na weapon-specific attack classifier (dominant wrist)\nand a shared guard classifier (off-hand wrist),\nreturning one ACTION_RESULT with both outcomes;
:Central Game Engine (a separate process on the same\nUltra96 board as the classifiers) fuses ACTION_RESULT\nwith the current POSITION for both players and\nresolves combat (HIT / MISS / BLOCK / PARRY);
fork
  :Unity Visualizers render the action, update HP/UI,\nand trigger VFX;
fork again
  :Laptop sends COMBAT_FEEDBACK to the FireBeetle,\nwhich relays it over ESP-NOW to the dominant wrist\nunit, driving the haptic motor and status LED locally;
end fork
stop

@enduml
```

## Fig 4.7 — Central Coordination Architecture

Placement: Section 4.7 "Central Coordination, Parallelism and Synchronization", at the Fig 4.7 placeholder at the end of that subsection.

```plantuml
@startuml fig_4_7_central_coordination
skinparam backgroundColor white
skinparam component {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}
skinparam queue {
  BackgroundColor #f1ecf9
  BorderColor #6a4c93
}
skinparam rectangle {
  BackgroundColor #f1ecf9
  BorderColor #6a4c93
}

title Fig 4.7 - Central Coordination Architecture (single asyncio event loop)

component "P1_sensor_ingest" as t1
component "P2_sensor_ingest" as t2
component "P1_ai_result" as t3
component "P2_ai_result" as t4
component "P1_position" as t5
component "P2_position" as t6

queue "P1 sensor queue" as q1
queue "P2 sensor queue" as q2
queue "P1 AI result queue" as q3
queue "P2 AI result queue" as q4
queue "P1 position queue" as q5
queue "P2 position queue" as q6

t1 --> q1
t2 --> q2
t3 --> q3
t4 --> q4
t5 --> q5
t6 --> q6

rectangle "Game State coroutine\n(sole writer: weapon, last action,\nlast position, HP per player)" as GameState

q1 --> GameState
q2 --> GameState
q3 --> GameState
q4 --> GameState
q5 --> GameState
q6 --> GameState

rectangle "Combat Resolution\n(on each new ACTION; timestamps\nadjusted by per-device clock offset,\ne.g. parry window +/-150ms)" as Combat
rectangle "Broadcast task\n(fans out COMBAT_EVENT to both\nvisualizer connections concurrently)" as Broadcast

GameState --> Combat
Combat --> Broadcast

component "Unity Visualizer P1" as v1
component "Unity Visualizer P2" as v2

Broadcast --> v1
Broadcast --> v2

note bottom of GameState
  P1_position / P2_position read directly off
  each player's Visualizer WebSocket connection,
  not the FireBeetle/Ultra96 path.
end note

@enduml
```

## Fig 6.2 — Visualizer Module Architecture and Data Flow

Placement: Section 6.2.2 "Module Breakdown", at the Fig 6.2 placeholder after the module bullet list.

```plantuml
@startuml fig_6_2_visualizer_module_architecture
skinparam backgroundColor white
skinparam componentStyle rectangle
skinparam component {
  BackgroundColor #eaf2fb
  BorderColor #2c5f8a
}
skinparam queue {
  BackgroundColor #f1ecf9
  BorderColor #6a4c93
}
skinparam rectangle {
  BackgroundColor #f1ecf9
  BorderColor #6a4c93
}

title Fig 6.2 - Visualizer Module Architecture and Data Flow

component "Central Game Engine\n(secure WebSocket, WSS)" as GE

package "Receive path" {
  component "WebSocket Client\n(reconnect, loss reporting)" as WS
  queue "Inbound Message Queue\n(thread-safe)" as Q
  component "Message Dispatcher\n(drained fully once per frame,\nmain thread)" as Disp
  component "Local Game State Mirror\n(read-only copy of\nauthoritative state)" as Mirror
  component "Avatar Controller" as Avatar
  component "Combat FX Controller" as FX
  component "HUD Controller" as HUD
  component "Screen State Manager" as Screen
}

package "Transmit path" {
  component "Position Publisher\n(pose sample, movement clamp,\nfixed-rate TX)" as Pub
}

rectangle "Coordinate Mapping\n(local tracking space <-> arena,\nboth directions)" as Coord
component "Mock Data Injector\n(synthetic messages,\nsame queue as live traffic)" as Mock
component "AR Foundation Session\n(camera passthrough + 6DoF VIO)" as ARF

GE --> WS : ACTION, WEAPON_CHANGED,\nCOMBAT_EVENT, POSITION,\ncooldown, match state
WS --> Q : parsed off the network thread
Mock --> Q : synthetic messages
Q --> Disp
Disp --> Mirror
Mirror --> Avatar
Mirror --> FX
Mirror --> HUD
Mirror --> Screen

ARF --> Pub : device pose
Pub --> Coord : local -> arena
Coord --> Pub
Pub --> WS : POSITION { x, y, heading }
WS --> GE

Coord --> Avatar : opponent arena position\nmapped back to local space
ARF --> FX : world-anchored placement

note bottom of Mirror
  The visualizer never computes combat
  outcomes locally, so both players'
  displays agree by construction.
end note

@enduml
```
