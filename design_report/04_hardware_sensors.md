# Section 3: Hardware Sensors

## 3.1 Overview of Hardware and Algorithmic Interaction

Each player wears 1 Wearable Sensor Pack, consisting of three physical units, each self-contained and wirelessly linked:

- **Dominant wrist-worn unit:** ESP32-C3 MCU, 1 IMU, 1 13.56 MHz RFID Reader, 1 Status LED, 1 Haptic motor, local LiPo battery
- **Non-dominant wrist-worn unit:** ESP32-C3 MCU, 1 IMU, local LiPo battery
- **Belt unit:** FireBeetle ESP32, battery, 1 IMU

Each wrist unit's ESP32-C3 samples its local sensor(s), performs on-unit pre-processing, and transmits the result wirelessly over ESP-NOW to the belt FireBeetle, which acts as the aggregation point. The pack operates a continuous sense-process-communicate-react loop entirely on the FireBeetle ESP32:

> **Fig 3.1 — Continuous sense-process-communicate-react loop.**

### 3.1.1 How Sensors Provide Inputs to Algorithms

Each sensor feeds a distinct algorithmic role: dominant-wrist IMU → attack-gesture classification; non-dominant-wrist IMU → defensive-state (block vs. relaxed); belt IMU → body reference frame for both wrists; RFID → weapon-ID lookup selecting the active move-set. Motion outputs from both wrists and the torso combine into one `SENSOR_FRAME` per interval.

### 3.1.2 How Algorithms Determine Actuator Behaviour

The pack's actuators (haptic motor, status LED, both on the dominant-wrist unit) are driven by event codes streamed from the Central Game Engine to the belt FireBeetle, which relays the relevant command over ESP-NOW to the dominant-wrist ESP32-C3. A local state machine there maps each code to a haptic waveform and LED pattern, keeping actuation deterministic and independent of upstream network jitter.

### 3.1.3 Emphasis on Real-Time Embedded Execution

The end-to-end latency budget is tight relative to human perception of responsiveness. Sensor sampling and pre-processing run in timer-driven ISRs / high-priority FreeRTOS tasks on one ESP32 core, decoupled from BLE stack activity on the other core. All on-pack algorithms are O(1) per sample or use small fixed-length sliding windows. Data is pushed at a fixed cadence (bounding worst-case latency to one frame interval) rather than polled. During idle periods, frames carry a minimal payload rather than being suppressed.

## 3.2 Sensor and Sensor-Side Algorithms

### 3.2.1 Sensor Selection and Purpose

| Sensor type and model | Physical quantity measured | Purpose & why it is suitable |
|---|---|---|
| Wrist & Torso 6-axis IMU MPU-6050 | 3-axis linear acceleration and angular velocity of the player's wrist and torso [3] | Swings, thrust and blocks are rotational motions of the hand. A weapon that is rigidly gripped moves 1:1 with the wrist. |
| MFRC522 13.56 MHz reader (RFID Reader) [4] | Presence and unique ID of a nearby passive tag | Cheap, fast, short-range, no line-of-sight needed. Detects weapon pickup instantly. |
| 13.56 MHz RFID tag | | |

### 3.2.2 Sensor Data Processing Algorithms

**IMU Processing:**

- **Startup calibration:** player holds the prop still for ~1 s at boot. Accel/gyro biases are averaged and subtracted from every subsequent sample.
- **Noise filtering:** a median filter rejects single-sample spikes, followed by a moving-average/low-pass filter that preserves swing/thrust dynamics.
- **Sensor fusion:** accelerometer and gyroscope are combined into a drift-corrected orientation quaternion.
- **Relative-frame transform:** torso IMU's orientation expresses wrist IMU's motion in a body-relative frame, isolating arm/hand movement from whole-body movement.
- **Motion-energy gating:** a gyroscope-magnitude threshold flags active motion windows; only these carry full-resolution samples, reducing average payload without violating the fixed cadence.

**RFID Processing:**

- **Debounce + ID lookup:** a tag read is only reported as `WEAPON_CHANGED` once the same tag ID is confirmed across 2–3 consecutive polls and differs from the last reported weapon (preventing false swaps from momentary misreads); the confirmed UID is then mapped to a compact weapon-ID enum via a local lookup table.

### 3.2.3 Sensor Interface and Timing Considerations

| Sensor | Local Interface | Uplink to Belt | Sampling Rate | Algorithmic Rate | Rationale |
|---|---|---|---|---|---|
| Wrist IMUs | I2C | ESP-NOW, 50 Hz | 200 Hz | 200 Hz filter/fusion; 50 Hz packaging | Resolves peak swing velocity; ESP-NOW avoids a persistent connection |
| Torso IMU | I2C | N/A | 100 Hz | 100 Hz fusion; 50 Hz transform | Slower motion. Wired since co-located with FireBeetle |
| RFID Readers | SPI | Shares dominant-wrist ESP-NOW uplink | ~5 Hz (2 Hz idle) | ~5 Hz | Pickup latency-tolerant. Rides same wireless hop as wrist IMU |

## 3.3 Actuators and Actuator Control Algorithms

### 3.3.1 Actuator Selection and Function

| Actuator | Type | Physical action | Function |
|---|---|---|---|
| Haptic motor | ERM coin motor, PWM via transistor | Vibration at wrist | Immediate feedback for hit/block/weapon-change events |
| Status LED | WS2812 RGB, ESP32 RMT | Colour/blink pattern | Visual pack-state indicator for setup/debug/demo |

### 3.3.2 Actuation and Control Algorithms

Both actuators are driven by a lightweight event-to-pattern state machine running on the dominant-wrist ESP32-C3 [5], triggered by commands relayed from the belt unit over ESP-NOW, decoupled from the sensing loop:

- **Haptic control:** incoming event codes select a pre-defined PWM pattern (duty %, duration, repeat count) from a lookup table (e.g. sharp pulse for `HIT_TAKEN`).
- **LED control:** a finite-state machine maps pack status to a colour/blink pattern, updated on local checks (battery) or Game Engine messages.

### 3.3.3 Relationship between Control Algorithm and Driver Hardware

The haptic motor cannot be driven directly from a GPIO pin at the current an ERM motor requires, so the control algorithm's PWM output drives the gate of a small N-channel MOSFET (or NPN transistor) that switches the motor's supply current, with a flyback diode across the motor to protect the switching transistor. The status LED, being an addressable (WS2812-type) device, integrates its own driver logic on-die; the control algorithm only needs to send a colour value over a single-wire protocol via the ESP32's RMT peripheral, with no external driver circuitry required.

## 3.4 Hardware-Algorithm Integration

| Sensor | Algorithm(s) | Output | Execution |
|---|---|---|---|
| Wrist IMUs | Offset calibration → median → low-pass → fusion → motion-gating; frame packaging → AES-128-CCM encrypt → ESP-NOW TX | Encrypted wrist motion frame (`SENSOR_FRAME` contribution) | Wrist ESP32-C3: 200 Hz sensing/fusion, 50 Hz TX |
| RFID (on dominant wrist) | RFID debounce → ID lookup → AES-128-CCM encrypt → ESP-NOW TX | Encrypted weapon-ID event (`RFID_EVENT`), sent only on confirmed change | Wrist ESP32-C3: ~5 Hz polling, event-driven TX |
| Torso IMU + Belt merge | Offset calibration → low-pass → fusion; ESP-NOW RX + decrypt (both wrists) → relative-frame transform → packetize | Combined body-relative frame | Belt FireBeetle Core 0: 100 Hz IMU, 50 Hz merge/packetize |
| Frame → laptop | BLE TX, fixed-cadence dispatch | Outgoing sensor frame to laptop relay | Belt FireBeetle Core 1, 50 Hz |
| Haptic motor | Belt relays event code via ESP-NOW → local pattern lookup → PWM drive | Vibration feedback pulse | Dominant-wrist ESP32-C3, event-driven |
| Status LED | Local state machine → colour/blink pattern | Visual status indication | Dominant-wrist ESP32-C3, event-driven |

## 3.5 Power & Safety Considerations

### 3.5.1 Estimated Power Budget

| Component | Typical active current | Idle current |
|---|---|---|
| Wrist ESP32-C3 (incl. ESP-NOW) | ~80–110 mA | ~20 mA |
| Belt FireBeetle (incl. BLE) [6] | ~120–160 mA | ~20 mA |
| IMU (MPU-6050) | ~3.5–4 mA | <0.1 mA |
| RFID reader | ~13–26 mA | ~10 µA |
| Haptic motor | ~60–90 mA (peak) | 0 mA |
| Status LED [7] | ~15–20 mA | ~1–2 mA |

Sustained draw: ~171–250 mA (dominant wrist), ~83.5–114 mA (non-dominant wrist), ~123–164 mA (belt), giving ~1.2–1.75 h dominant wrist runtime, ~2.6–3.6 h non-dominant wrist runtime and ~5.5–7.3 h belt runtime.

### 3.5.2 Power Sources and Voltage Regulation

- **Power sources:** three independent 3.7 V single-cell LiPo batteries per pack (one ~900 mAh cell in the belt unit, and one ~300 mAh cell in each wrist unit).
- **Regulation:** each unit's onboard regulator (ESP32-C3 or FireBeetle) steps its local battery down to 3.3 V for that unit's MCU and peripherals; decoupling capacitors suppress transient noise on each unit's local rail independently, since there is no shared rail across units.
- **Haptic motor supply:** driven from the dominant wrist unit's raw battery rail (not its 3.3 V logic rail) to avoid brownout during pulses.

### 3.5.3 Safety Considerations and Mitigations

- Wrist batteries sit closest to impact during swings, leading to puncture/crush/over-charge risk. Use small (~300 mAh) cells in rigid enclosures off the striking edge, each with its own TP4056 protection circuit [8].
- Electrical contact with skin/sweat might cause minor shock/corrosion risk. Mitigate by using fully enclosed housings with no exposed contacts.

## 3.6 Assumptions, Constraints and Risks

### 3.6.1 Environmental Assumptions

- The play area has enough physical space for full-arm swings without collision hazards to bystanders or equipment.
- The 2.4 GHz band carries BLE (FireBeetle↔laptop) and ESP-NOW (wrist↔belt) simultaneously; ESP-NOW is assumed to operate on a fixed channel separate from BLE's frequency-hopping range to limit mutual interference.

### 3.6.2 Measurement Limitations

- The wrist-IMU design assumes the weapon is gripped rigidly with the hand. A loose grip would not be captured accurately. This is judged an acceptable trade-off for the reduction in cost, weight and per-weapon duplication it buys, but should be validated with testing.
- Gyroscope drift is unconstrained without a magnetometer, especially in yaw. Periodic re-zeroing between rounds keeps errors bound within a match.
- Similar gesture pairs (e.g. diagonal vs. vertical swing) can produce similar IMU signals; hardware supplies clean signals, but final disambiguation is the Ultra96 ML classifier's responsibility.
- RFID's short read range assumes tags are embedded close enough to the expected hand position for reliable, false-trigger-free detection.

### 3.6.3 System Assumptions and Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Wrist↔belt ESP-NOW link | Channel congestion, dropped frames | Fixed ESP-NOW channel separate from BLE hopping; tolerant of occasional drops |
| End-to-end latency chain | Sensor pack meeting its own latency budget does not guarantee the full chain meets the sub-100 ms perceptual target | Pack is designed to contribute only ~1 BLE frame interval (~20 ms) of latency, leaving the majority of the budget for downstream classification and rendering; should be validated with end-to-end latency testing, not assumed. |
| Wrist battery depletion | ~2–2.5 h runtime, bottleneck vs. belt's ~6 h | Per-unit `LOW_BATTERY` event; spares kept nearby for quick swap. |

## 3.7 Hardware Component Summary Table

| Component | Type | Purpose | Interface | Key Considerations |
|---|---|---|---|---|
| FireBeetle ESP32 ×2 | Microcontroller (dual-core, BLE/WiFi) | Sensor sampling, on-pack processing, framing and BLE transmission; actuator control | SPI / I2C (sensors), BLE (comms) | Schedules sensing/radio across both cores; onboard 3.3 V regulator |
| Wrist MCU (ESP32-C3) ×4 | Microcontroller (WiFi/ESP-NOW) | Local sensor sampling + pre-processing + wireless uplink | I2C/SPI (local), ESP-NOW (uplink) | No wired link to belt; own battery/regulator per unit |
| Wrist IMU ×4 (MPU-6050) | 6-axis IMU | Wrist motion for gesture recognition | I2C to local ESP32-C3 | Data reaches belt via ESP-NOW |
| Torso IMU (MPU-6050) ×2 | 6-axis IMU | Provide body-reference orientation to isolate wrist motion from body motion; supply orientation for combat resolution | I2C, 100 Hz | Mounting stability on the torso affects reference-frame accuracy |
| RFID Readers (MFRC522) ×2 | 13.56 MHz reader | Weapon-ID detection | SPI to dominant-wrist ESP32-C3 | Rides dominant-wrist ESP-NOW uplink |
| Passive RFID Tags ×6 | 13.56 MHz passive tag | Uniquely identify Sword & Shield / Spear / Mace props | Contactless (read by MFRC522) | Embedded close to expected reader position; no battery; passive |
| Haptic Motor (ERM + driver) ×2 | Vibration actuator | Physical feedback for hit/block/parry/weapon-change events; dominant-wrist unit | GPIO/PWM via transistor | Driven from raw battery rail to avoid brownout |
| Status LED (WS2812-type) ×2 | Addressable RGB LED | Visual indication of pack state for setup/debug/demo; dominant-wrist unit | Single-wire (ESP32 RMT peripheral) | Kept dim in idle states to conserve power |
| LiPo Batteries ×6 | Rechargeable cells | Power per unit | N/A | 1×900 mAh (belt), 2×300 mAh (wrists) — per player |
| Charge/Protection Modules ×6 | TP4056-based | Safe charging & protection | USB/JST | One per battery, not shared |
