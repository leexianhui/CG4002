# CG4002 Computer Engineering Capstone Project

## Design Report

**Group 23**

| | Name | Student # | Primary Component | Secondary Component |
|---|---|---|---|---|
| Member #1 | Wong Xin Hui | A0281926W | Hw Sensors | Sw/Hw AI |
| Member #2 | Jiaming | A0282057A | Sw Visualizer | Comms |
| Member #3 | Xian Hui | A0227521A | Comms | Sw Visualizer |
| Member #4 | Kankanam Gamage Himeth Thewmika | A0287216Y | Sw/Hw AI | HW Sensors |

---

## Table of Contents

- **Section 0 — Introduction**
  - Background & Context
  - Problem Statement
  - Motivation & Significance
  - Objectives
- **Section 1 — System Functionalities**
  - 1.1 Actors
  - 1.2 Feature List
- **Section 2 — Overall System Architecture**
  - 2.1 High-Level Architecture
  - 2.2 Intended Final Form
  - 2.3 AR Processing Overview
- **Section 3 — Hardware Sensors**
  - 3.1 Overview of Hardware and Algorithmic Interaction
  - 3.2 Sensor and Sensor-Side Algorithms
  - 3.3 Actuators and Actuator Control Algorithms
  - 3.4 Hardware-Algorithm Integration
  - 3.5 Power & Safety Considerations
  - 3.6 Assumptions, Constraints and Risks
  - 3.7 Hardware Component Summary Table
- **Section 4 — Communications**
  - 4.1 FireBeetle Setup and Configuration
  - 4.2 FireBeetle to Laptop Protocol
  - 4.3 Reliability
  - 4.4 Secure Communication
  - 4.5 Laptop to Ultra96 Communication
  - 4.6 Ultra96 to Visualizer Communication
  - 4.7 Central Coordination, Parallelism and Synchronization
- **Section 5 — Software/Hardware AI**
  - 5.1 Ultra96 Synthesis and Simulation Setup
  - 5.2 Neural Networks Explored
  - 5.3 Training and Testing
  - 5.4 Realizing the Models on the FPGA
  - 5.5 Evaluating Timing, Power, and Area
- **Section 6 — Software Visualizer**
  - 6.1 Visualizer Display
  - 6.2 Visualizer Software Architecture
  - 6.3 Overlaying Information on the Camera Feed
  - 6.4 Enabling AR of the Various Events
  - 6.5 Display Hardware
- **Section 7 — Project Management Plan**
- **References**

---

## Figures

All figures are authored as PlantUML source in [`image_prompts.md`](image_prompts.md) and rendered to
images that are pasted into the Google Doc. There are no checked-in SVG files — the PlantUML source is
the single point of truth so diagrams stay in sync with the text.

| Figure | Section | Source |
|---|---|---|
| Fig 2.1 — Overall System Deployment Diagram | 2.1 | `image_prompts.md` |
| Fig 2.2 — Intended Wearable Configuration | 2.2 | hand-drawn sketch (not PlantUML) |
| Fig 3.1 — Sense-Process-Communicate-React Loop | 3.1 | `image_prompts.md` |
| Fig 4.1 — System Communication Architecture | 4.1 | `image_prompts.md` |
| Fig 4.2.1 — FireBeetle to Laptop Handshake | 4.2 | `image_prompts.md` |
| Fig 4.2.2 — Packet Format | 4.2 | `image_prompts.md` |
| Fig 4.3 — Reliability Mechanisms | 4.3 | `image_prompts.md` |
| Fig 4.4 — Secure Communication Architecture | 4.4 | `image_prompts.md` |
| Fig 4.6 — End-to-End Data Pipeline | 4.6 | `image_prompts.md` |
| Fig 4.7 — Central Coordination Architecture | 4.7 | `image_prompts.md` |
| Fig 6.1 — Gameplay Display Layout | 6.1.1 | mockup |
| Fig 6.2 — Visualizer Module Architecture and Data Flow | 6.2.2 | `image_prompts.md` |
| Fig 6.3 — Headset Optics and Enclosure Requirements | 6.5 | sketch |
