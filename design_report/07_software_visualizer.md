# Section 6: Software Visualizer

The software visualizer is the player facing surface of the system. It runs on each player's phone, mounted in an enclosed headset, and carries two responsibilities. The first is to render the opponent, the combat outcomes and the player's own combat state over a live camera feed. The second is to act as a source of position data: the wearable sensor pack contains no positioning sensor, so the phone's own tracking is where each player's position in the arena comes from, and the visualizer transmits it continuously for use in combat resolution.

## 6.1 Visualizer Display

### 6.1.1 Information Displayed

The display is deliberately sparse. A player wearing a headset and swinging a physical prop has little attention to spare, so the gameplay screen carries only what changes the player's next decision:

- **Opponent avatar:** an animated character at the opponent's mapped position, holding their current weapon and performing their current action.
- **Own and opponent health:** two bars, always visible, updated on every confirmed combat outcome.
- **Active weapon indicator:** which of the three props the system believes the player is holding. A misread means the wrong classifier and move set, so it must be visible immediately rather than inferred from unexplained misses.
- **Guard state indicator:** whether the off hand currently reads as guarding. Guarding is a continuous state rather than an event, so without an indicator the player cannot tell whether a block will be honoured.
- **Cooldown readout:** four slots showing which moves are available and how long the rest have left.
- **Combat feedback:** distinct treatments for hit, miss, block and parry, plus weapon trails.

Everything else is moved off the gameplay screen. Match start, victory, defeat and any notifications are presented on separate screens rather than as a persistent banner, on the grounds that a banner occupies permanent space to convey information that changes only a handful of times per match.

> **Fig 6.1 — Gameplay display layout.**

### 6.1.2 Placement of the Phone Visualizer

The phone is mounted in landscape orientation inside an enclosed cardboard headset with one lens per eye, leaving both hands free for the weapon prop and the off hand guard. Because each lens covers one half of the screen, the display is divided down the middle, and the scene is rendered once with that single image copied into both halves rather than rendered twice from offset viewpoints. Both eyes therefore receive the same image. The headset construction and the reasoning behind this arrangement are covered in Section 6.5.

### 6.1.3 Design Constraints

- **Usable area per eye.** Each eye sees only half the display, and the circular lens aperture crops the corners of that half, so interface elements are kept away from the edges. It is the screen area that is constrained, not angular field of view.
- **Latency.** The visualizer sits at the end of the chain and cannot recover time lost upstream, so it applies every received message on the next rendered frame rather than deferring it.
- **Thermal and battery load.** Passthrough, tracking, rendering and an open connection together approach the phone's sustained thermal ceiling, so rendering settings are kept modest: low polygon avatar, restrained effects, capped frame rate.
- **Client networking.** The secure WebSocket connects to a local server with a self signed certificate, which on iOS requires an App Transport Security exception and the local network permission. Without them the connection fails silently.

## 6.2 Visualizer Software Architecture

### 6.2.1 Framework and Libraries

The visualizer is built in Unity with AR Foundation. AR Foundation provides camera passthrough and six degree of freedom device tracking through the platform's native AR backend, which is what supplies both the background image and the player position data. Unity is chosen because the animation, particle and interface tooling required for combat feedback is available directly rather than hand built, because AR Foundation abstracts the underlying AR platform so the build is not tied to one device family, and because its WebSocket client support is mature enough to carry the live connection without additional dependencies.

### 6.2.2 Module Breakdown

The visualizer is organised as a receive path, a transmit path, and a coordinate mapping utility used by both.

- **WebSocket Client:** maintains the single secure bidirectional connection, handles reconnection, and reports connection loss.
- **Inbound Message Queue and Message Dispatcher:** accept messages off the network thread and route them, on the main thread, to the responsible controller.
- **Local Game State Mirror:** a read only copy of the authoritative state. The visualizer never computes combat outcomes itself, so both players' displays agree by construction.
- **Avatar Controller:** drives the opponent's position, orientation, animation state and held weapon model.
- **Combat FX Controller:** plays impact, block and parry effects and weapon trails.
- **HUD Controller:** maintains health, guard state, active weapon and the cooldown readout.
- **Screen State Manager:** switches between the gameplay screen and the separate result and notification screens, and suspends effects when a match is not running.
- **Position Publisher:** samples the device pose, applies the movement clamp described in Section 6.3.3, converts it into arena coordinates and transmits at a fixed rate.
- **Coordinate Mapping:** holds the transform between the phone's own tracking space and the shared match arena, in both directions.
- **Mock Data Injector:** feeds synthetic messages into the same inbound queue used by the live connection.

> **Fig 6.2 — Visualizer module architecture and data flow.**

### 6.2.3 Data Streamed To and From the Phone

The visualizer exchanges data with the Game Engine only, over a single secure WebSocket connection, and does not communicate with the FireBeetle, laptop or Ultra96 directly. Inbound it receives the opponent's action and guard state, weapon changes, combat outcomes with resulting health, cooldown state, the opponent's position and heading, and match state. Outbound it transmits the player's own position and heading, sampled from the AR session, clamped and mapped into arena coordinates. This is the system's only source of player position and is used to gate range dependent combat outcomes. Every message carries a schema version, timestamp and player ID, consistent with Section 4.6. How each inbound category is rendered is given in Section 6.4.1.

### 6.2.4 Threading and Frame Cadence

Messages arrive on a background network thread, but Unity's scene may only be modified from the main thread. Inbound messages are parsed off thread, placed into a thread safe queue, and drained once per rendered frame by the dispatcher, which then invokes the relevant controllers. A single point applies to all changes, so ordering is well defined and no locking is required inside the controllers. The queue is drained completely each frame rather than one message per frame, so a brief burst of traffic is absorbed within a single frame instead of visibly lagging the opponent.

### 6.2.5 Mock Data Mode

The mock injector produces synthetic actions, weapon changes, combat outcomes, cooldown state and opponent positions, and pushes them through the same queue and dispatcher as live traffic. This allows the complete visualizer, including animation, effects, screen transitions and interface behaviour, to be built and demonstrated before any sensor hardware exists. It also provides a deterministic way to reproduce a specific combat sequence when debugging, which live hardware cannot.

## 6.3 Overlaying Information on the Camera Feed

### 6.3.1 Layer Composition

The AR session supplies camera frames, which are drawn as the background before any virtual content. Virtual content is then composited in two further layers, and the completed image is copied into both halves of the display.

The separation between the two virtual layers is a design decision rather than an implementation detail. World anchored content, meaning the avatar, its weapon, trails and impact effects, is placed in AR space and stays fixed relative to the room as the player turns their head, which is what makes the opponent feel physically present. Screen space content, meaning health, guard state, weapon and cooldowns, is fixed to the viewport and does not move with the head, so it stays readable during vigorous motion. Keeping the layers separate also contains failure: if tracking degrades, only the world anchored layer is affected, and the player's own combat state remains correct and legible.

### 6.3.2 Mapping Between Tracking Spaces

Each phone begins its AR session with an origin wherever the application happened to initialise, and with an arbitrary heading. Two phones therefore report positions in two unrelated coordinate systems, and a position from one is meaningless to the other. Since combat resolution gates hit on the distance between players, this must be resolved before any position is exchanged.

An alignment step at the start of each match establishes the relationship once. Both players stand on marked spawn positions facing each other and press ready, and each visualizer records the fixed offset and rotation between its own tracking space and the shared match arena. From then on every outgoing position is expressed in arena coordinates, and every incoming opponent position is mapped back into the local tracking space before the avatar is placed.

### 6.3.3 Tracking Stability and Movement Clamping

Position tracking is performed by visual inertial odometry, which follows recognisable features in the camera image across frames and fuses this with the phone's inertial measurements, and therefore depends on the play area containing enough visual detail to track against.

Two distinct forms of error need separating. A systematic scale error, where a player walks a given real distance and the system reports a slightly different one, is largely tolerable, because gameplay is adjudicated in arena coordinates and both players are subject to the same geometry. Instability is the more damaging failure: if the reported position drifts or jumps while a player is standing still, their avatar appears to slide or teleport on the opponent's screen, which reads immediately as a broken system. The Position Publisher therefore applies a clamp limiting how far a reported position may move between updates, so that a tracking glitch degrades into brief lag rather than a visible jump. Because players share a physical space, accumulated drift also causes the avatar to separate gradually from the real opponent visible through passthrough, so a re alignment on the spawn marks between rounds is planned, and the rate of drift over a realistic match length should be measured rather than assumed acceptable.

## 6.4 Enabling AR of the Various Events

### 6.4.1 Event to Visual Response Mapping

Every visible behaviour is triggered by a message from the Game Engine. Nothing is inferred locally, so both players always see the same adjudicated outcome.

| Event received | Visual response | Render layer |
|---|---|---|
| Weapon change | Avatar swaps to the corresponding weapon model. The player's own weapon indicator and cooldown slots update to that weapon's move set. | World and screen |
| Opponent attack action | Avatar plays the matching attack animation for that weapon and action class, with a weapon trail along the swing. | World |
| Opponent guard state | Avatar holds or releases the guard pose on the off hand. | World |
| Own guard state | Guard indicator switches between active and inactive. | Screen |
| Combat outcome, hit | Impact effect at the point of contact, hit reaction on the avatar, health bar decreases, and the damage value is shown briefly. | World and screen |
| Combat outcome, block or parry | Distinct deflection effect and a shorter recoil animation, visually separable from a hit so the player can tell why no damage was dealt. | World and screen |
| Cooldown update | Affected cooldown slot begins or finishes counting down. | Screen |
| Opponent position update | Avatar target position and heading updated, then smoothed toward. | World |
| Match state change | Transition to the result or notification screen. | Screen |

### 6.4.2 Avatar Animation State

The avatar is driven by a state machine with an idle state per weapon, one attack state per action class recognised for that weapon, a guard state and a hit reaction state. The action classes are those the classifiers distinguish, so the animation set is bounded by what the system can actually detect: six attack states for sword and shield, four for spear, four for mace, plus the shared guard and reaction states. Because attacks are subject to cooldowns, successive actions cannot arrive faster than the cooldown allows, so overlapping animations are rare. Where an action does arrive while another is still playing, the current animation is interrupted rather than deferred.

### 6.4.3 Position Smoothing

Position updates arrive at network cadence rather than frame rate, so applying them directly would make the avatar step visibly between discrete locations. The Avatar Controller treats each received position as a target and interpolates toward it over the following frames, using the reported heading to orient the avatar. The interpolation window is kept short: long enough to remove the stepping, short enough that the rendered position does not lag far behind the position used to adjudicate the exchange, since a visible mismatch between where the opponent appears and where a hit registers would undermine the sense that outcomes are fair.

## 6.5 Display Hardware

The headset is a cardboard shell of the Google Cardboard type, carrying one biconvex lens per eye at a focal length of approximately 45 mm. Two construction details matter for this design. The shell requires an opening at the rear camera position, since viewers of this type normally enclose the phone completely and would block the passthrough feed the visualizer depends on. It also requires a septum, a divider running down the centre between the eyes, without which each eye can see part of the opposite half at an oblique angle and receives an unfocused ghost image.

The same image is presented to both eyes, a configuration described in the literature as biocular. It is preferred here because it halves the per frame rendering cost, avoids a second extended reality plugin that does not coexist reliably with AR Foundation, and removes the need to tune eye separation for viewer comfort. The trade off is the absence of a binocular depth cue, recorded in Section 6.1.3.

> **Fig 6.3 — Headset optics and enclosure requirements.**
