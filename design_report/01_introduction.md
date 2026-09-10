# Section 0: Introduction

## Background & Context

Motion-sensing games and embedded machine learning have converged rapidly in recent years: wearable IMUs are now cheap and accurate enough to capture fine-grained human motion in real time [1], while edge AI accelerators (FPGAs) make it feasible to classify that motion with low latency without relying on cloud inference [2]. Separately, augmented reality (AR) has matured from novelty demos into consumer-usable overlays on phones and headsets, letting virtual content be convincingly composited onto the real world. Physical, prop-based combat games, historically the domain of live-action role play (LARP) with foam weapons and human referees, have not kept pace with this technology. Hit detection, damage, and "who won the exchange" are still adjudicated by eye, which is slow, subjective, and unable to scale to nuanced mechanics like blocking, parrying, or weapon-specific move sets. This project sits at the intersection of these three trends: wearable motion sensing, FPGA-accelerated gesture classification, and AR visualization, applied to a fast-paced physical activity (medieval melee combat) that has historically resisted digitization.

## Problem Statement

Existing motion-controlled or AR combat experiences typically fall into one of two unsatisfying categories:

1. **Marker/camera-tracked systems** (e.g. motion-capture rigs, optical AR combat installations) that require fixed external cameras, controlled lighting, and significant infrastructure. They do not work for casual or portable play, and are expensive to design & set up.
2. **Simplified motion-controller games** (e.g. VR fitness/combat titles) that abstract the weapon into a generic handheld controller, discarding the physicality of holding a distinct sword, spear, or mace, and typically use a single coarse gesture classifier rather than weapon-specific move sets.

What is missing is a self-contained, wearable, low-latency system that (a) identifies which physical weapon a player is holding, (b) classifies weapon-appropriate combat actions from on-body motion sensors alone (no external cameras), (c) fuses this with real-time relative positioning between two players to adjudicate range-dependent outcomes (hit/miss/block/parry), and (d) renders the result back to each player as a believable AR opponent avatar, all within a latency budget that preserves the feel of real-time combat.

## Motivation & Significance

Beyond the novelty of the game itself, this project is a natural vehicle for demonstrating the full breadth of skills targeted by CG4002: embedded sensor acquisition and signal processing, a custom wireless communication protocol under real-time and reliability constraints, dataset collection and FPGA-accelerated neural network deployment, and an AR software client, integrated end-to-end into a single working multiplayer system rather than four disconnected subsystems. It also has genuine appeal as a demonstrable, replayable exhibit: unlike single-user gesture-recognition demos, a 1v1 duel is immediately legible to spectators and creates a natural stress-test of the system's real-time and multi-device synchronization properties.

## Objectives

This project aims to:

- Design and implement wearable sensor nodes (IMU, RFID on a FireBeetle ESP32) that capture motion and weapon identification for two simultaneous players.
- Design and implement a communication protocol and transport-agnostic messaging layer connecting sensor nodes, FPGA inference hosts, a central game-logic coordinator, and two AR visualizer clients, addressing reliability, security, and synchronization across all links.
- Train, evaluate, and deploy three weapon-specific gesture classification models (sword & shield, spear, mace) on the Ultra96 FPGA platform, meeting a real-time inference latency budget.
- Design and implement a combat resolution engine that fuses classified actions, weapon type, and inter-player spatial data into game outcomes (hit, miss, block, parry).
- Implement a Unity-based AR visualizer that renders an animated opponent avatar, combat feedback, and game state overlaid on a live camera feed for each player.
- Integrate and evaluate the complete system as a functioning two-player duel, including a mock-data pathway that allows each subsystem to be developed and tested independently before full hardware integration.
