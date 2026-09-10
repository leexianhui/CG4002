# Section 2: Overall System Architecture

## 2.1 High-Level Architecture

**Fig. 2.1** shows the overall deployment architecture. Each player wears a sensor pack comprising two wrist units and a belt-mounted FireBeetle ESP32. The wrist units collect IMU data while the dominant wrist also detects weapon identity through RFID. Sensor data is aggregated by the FireBeetle and forwarded to the player's laptop. Each laptop acts purely as a communications relay: it forwards sensor windows to a single, shared Ultra96 FPGA for action and guard classification. That same Ultra96 also hosts the Central Game Engine as a second process, maintaining the authoritative game state and resolving combat outcomes. The Game Engine then distributes updates to both AR visualizers and feedback commands to the wearable devices, all as direct connections to the Ultra96.

Consolidating the Game Engine onto the Ultra96, rather than a player's laptop, is a deliberate response to a network constraint at the venue: the school WiFi enforces client isolation between personal devices, so a laptop or phone cannot open a direct connection to another laptop or phone even though they share the same network. Only a small set of whitelisted devices can act as a sender to arbitrary other devices on that network, and the Ultra96 is provisioned as one of them. Hosting the engine there means every other device (both laptops, both phones) only ever needs a direct connection to the Ultra96, never to each other, which is compatible with that restriction. This is why the earlier design's "Player 1's laptop hosts the engine" arrangement, which required Player 2's laptop and both visualizers to reach Laptop P1 directly, has been dropped. Section 4.5 discusses how the Ultra96 is provisioned as a whitelisted device on the venue WiFi for a live match, as distinct from its normal placement on the NUS SoC Research Subnet during development.

> **Fig 2.1 — Overall System Deployment Diagram.** PlantUML source: `image_prompts.md`, section "Fig 2.1".

## 2.2 Intended Final Form

**Fig. 2.2** illustrates the intended wearable configuration. The dominant wrist unit contains an IMU, RFID reader, haptic motor, status LED, and battery, while the non-dominant wrist unit contains an IMU and battery for guard-state sensing. A belt-mounted FireBeetle and torso IMU act as the local processing and communication hub. The player also wears a phone-based AR headset, which provides both visual feedback and position tracking for combat resolution.

> **Fig 2.2 — Intended Wearable Configuration.** Hand-drawn/AI-generated labelled sketch (front/back view). Not a PlantUML diagram — see the note at the top of `image_prompts.md`.

## 2.3 AR Processing Overview

The AR subsystem continuously tracks the player's pose using AR Foundation and converts local device coordinates into a shared arena coordinate system established during match initialization. Position updates are transmitted to the Game Engine, while combat and state updates are received through a secure WebSocket connection. The visualizer then updates the opponent avatar, weapon model, animations, combat effects, and user interface elements before compositing them onto the live camera feed, providing players with a real-time augmented view of the duel.
