# Camera Side: Tracking, Placement and the Sword Prop

How the visualizer uses the camera: tracking the room and the sword prop, placing the dummy, and
reporting this player's position.

Open in VS Code with `Cmd+K V` so the diagrams line up.

---

## 1. Two kinds of tracking

| | **World tracking** | **Image tracking** |
|---|---|---|
| Question it answers | Where is the phone in the room? | Where is the sword prop? |
| How | ARKit visual-inertial odometry: follows feature points in the camera image and fuses them with the phone's gyroscope and accelerometer | ARKit matches a known picture (the marker on the blade) in each camera frame |
| Gives us | The camera's position and rotation every frame | The marker's position and rotation every frame |
| Visible effect | The dummy stays fixed on the floor as you walk around it | The sword model stays on the cardboard as you move it |
| Our code | `DummyPlacer` (floor detection, tap to place), `PositionReporter`. The camera pose itself needs no code: `XR Origin` + `AR Session` | `PropTracker`, `SwordHitDetector`, `SwordTrail`, `TrackedImageDebug` |

World tracking is why anything can be *placed* in the room at all. A virtual object is given a
position in world coordinates, and because ARKit keeps updating the camera's own pose, the object
is redrawn from the right viewpoint every frame and appears fixed in the room.

As part of world tracking, ARKit also finds **planes**: flat surfaces such as the floor, built up
from feature points that lie on a common level. These are what the dummy is placed on.

---

## 2. Pipeline, one camera frame

```
 camera frame + IMU
        │
        ▼
 ┌──────────────────────── ARKit (on the phone) ────────────────────────┐
 │  world tracking: camera pose,       image detection: marker pose     │
 │  floor planes                                                        │
 └───────────────┬──────────────────────────────────┬───────────────────┘
                 ▼                                  ▼
          AR Camera moves                ARTrackedImageManager
          (XR Origin)                    trackablesChanged: added / updated
          ARPlaneManager → DummyPlacer
          (tap a plane → dummy placed)
          PositionReporter → Outbound
                 │                                  │
                 │                    ┌─────────────┼─────────────────┐
                 │                    ▼             ▼                 ▼
                 │             PropTracker   TrackedImageDebug   (Shield marker:
                 │             sword model   green/yellow         unused in the
                 │             on the prop   outline + readout    sword-only demo)
                 │                    │
                 │                    ├──▶ SwordTrail: slash trail when the tip moves fast
                 │                    ▼
                 │             SwordHitDetector ── blade touched dummy? ──▶ Outbound (offline only;
                 │                                                    see StateEngine.md)
                 ▼
          render: camera image as background, then dummy, sword, effects on top
```

---

## 3. The reference image library

`Assets/CG4002/AR/PropMarkers.asset`, built by **CG4002 → Setup Image Tracking**
(`Editor/ImageTrackingSetup.cs`).

| Name | Image | Printed size | Used for |
|---|---|---|---|
| `Sword` | `two.png` | 9 × 9 cm | the sword prop |
| `SwordBackup` | `one.png` | 9 × 9 cm | also the sword, if `two` tracks badly |
| `Shield` | `Rafflesia.jpg` | 15 × 10 cm | unused in the sword-only demo |

- **Why the physical size matters:** a camera sees only an image's size *in pixels*. The same
  picture looks the same whether it is small and close or big and far. Telling ARKit the real
  width lets it work out the distance. A wrong size puts the sword model at the wrong depth.
- **Why the images are baked into the app:** ARKit prepares each reference image when the app is
  built. The app can only recognise images that were in the library at build time.
- **Why "moving images = 2":** by default ARKit assumes a detected image is fixed to the wall or
  table, and stops updating it closely. Declaring moving images makes it track them every frame,
  which a prop in your hand needs.
- **Why the import is set to "no resizing":** Unity stretches textures to power-of-two sizes by
  default, which would distort the reference compared with the print.

---

## 4. From marker to sword (`AR/PropTracker.cs`)

ARKit reports where the **marker** is. We want the **sword**. Two things are needed: the frames,
and the measurements.

```
   MARKER FRAME (from AR Foundation)          WEAPON FRAME (our models)

         +Z  towards the top of the picture         +Y  towards the tip
          ▲  (glued pointing up the blade)          ▲
          │                                          │
    ┌─────┼─────┐                              ─────┼─────  crossguard
    │     │  2  │──▶ +X                              │
    │     ●     │                                    ●  pivot = middle of the grip
    └───────────┘                                    │
     +Y points out of the print, at the camera     +Z is the flat of the blade
```

So the mapping is: weapon **up** = marker **+Z** (up the blade), weapon **forward** = marker **+Y**
(out of the flat). In code that is one line: `Quaternion.LookRotation(marker.up, marker.forward)`.

**Measurements** place and size the model:

```
      tip ─────────────┐
                       │  markerToTip
      marker centre ●──┤
                       │  markerToGripCentre
      grip centre   ●──┘  ← the model's pivot goes here
```

- **Position:** `grip = marker.position - upTheBlade × markerToGripCentre`
- **Scale:** the model is scaled so its tip lands `markerToTip` beyond the marker:
  `scale = (markerToGripCentre + markerToTip) / (model's grip-to-tip length, 0.95 m)`
- `markerToGripCentre` is **measured** on the prop, so the rendered handle sits in your hand.
- `markerToTip` is a **choice**: where the rendered tip should be. It can extend past the
  cardboard. With the two summing to about 0.95 the model keeps its natural size; since scaling
  is uniform, a much longer sword also gets a wider blade.
- Hit detection uses the *rendered* blade, so hits land where you see the blade touch the dummy.
- Both are Inspector fields on **XR Origin → Prop Tracker**; no code changes needed.

### Tracking states, and what the sword does in each

| ARKit state | Meaning | Sword model |
|---|---|---|
| **Tracking** | Pose updated this frame | Follows the marker, smoothed |
| **Limited** | Image recognised but pose not updating (blur, steep angle) | Holds its last pose for `holdSeconds` (0.3 s), then hides |
| **None** | Not seen | Hidden |

- **Why hold for 0.3 s:** tracking often drops for a few frames during movement. Hiding instantly
  makes the sword flicker; holding briefly hides the gap.
- **Why snap on re-appearing:** after a gap the prop may be somewhere else entirely. Gliding there
  would look like the sword flying; snapping puts it straight back on the prop.
- **Smoothing:** each frame the model moves a fraction `t = 1 − e^(−followSpeed·Δt)` of the way to
  the marker. That removes small jitter, and because it depends on Δt, it behaves the same at 30
  or 60 fps. `followSpeed` is high (25) because the sword must feel attached to the prop.

---

## 5. Placing the dummy (`AR/DummyPlacer.cs`)

```
 app starts ──▶ dummy hidden, detected planes drawn (faint fill, green outline)
                   │
                   │  tap a plane
                   ▼
              raycast from the tap into the room  ──▶  hit point on the floor
                   │
                   ▼
              Arena moved to the hit point, turned to face you (vertical axis only)
              dummy shown, planes hidden, START_MATCH_REQUEST sent
```

- **Raycast:** ARKit shoots a ray from the camera through the tapped pixel and reports where it
  meets a detected plane. `PlaneWithinPolygon` counts only hits *inside* a plane's outline, so the
  dummy can't land on a guessed surface that isn't really there.
- **Why placing requests a new match:** the dummy's `AvatarController` only listens while it's
  active. Anything sent before placement (such as its starting weapon) wasn't heard, and a new
  match makes the engine send the whole starting state again.
- **Hiding planes** deactivates their GameObjects. ARKit keeps tracking them and raycasts still
  hit them; they just aren't drawn. Planes found later start hidden too.
- **MOVE DUMMY** (debug panel) hides the dummy and shows the planes again, for a new tap.
- **Dummy Height** (default 1.44 m, on **Game → Dummy Placer**) draws the 1.8 m model smaller. At
  sword's reach, about 1 m away, the camera sees only about a metre of height, so a full-size
  dummy fills the screen. The hit capsule scales with it.
- **Buttons vs. taps:** a tap on a HUD button is not also a placement; `IsOverUi` checks first.

---

## 6. Reporting this player's position (`AR/PositionReporter.cs`)

Once the arena is placed, 10 times a second, the camera's pose is converted into arena
coordinates and sent outbound as a `PositionMessage`:

- **Position:** `arena.InverseTransformPoint(camera.position)`, in metres from where the arena
  was placed. y is the phone's height above the floor.
- **Heading:** the camera's forward direction in arena space, flattened onto the floor, as degrees
  clockwise from the arena's +z. It is the same convention `AvatarController` uses to turn the
  opponent avatar, so one player's outbound position can be the other's inbound one.

The arena origin is wherever *this* phone placed it, so two phones only agree if they place it
on the same physical spot. That is an open team decision (StateEngine.md, section 7).

---

## 7. Sword contact to hit (`Offline/SwordHitDetector.cs`, offline only)

The dummy's body is approximated by an **upright capsule**: a cylinder with rounded ends. That's
the simplest shape that fits a standing body.

```
          ╭───╮  capsuleTop 1.7 m
          │   │
   blade ─┼───┼────●  tip          checked each frame:
          │   │                     (1) points along the blade, base → tip
          │   │    ○ previous tip   (2) points along the path the tip moved since last frame
          │   │
          ╰───╯  capsuleBottom 0.3 m      radius 0.25 m
            ▲
         dummy's feet
```

A point is inside the capsule if its distance to the capsule's centre line is less than the
radius. The heights and radius above are for a full-size dummy; they are scaled by the dummy's
actual scale, so the capsule always matches what you see. Points are checked every half-radius (12.5 cm) along each segment, so however far a fast
swing moves between frames, it can't jump over the body unchecked.

- **Why check the path, not just the blade:** at 30 fps a fast swing can move the tip half a
  metre between frames, from one side of the dummy to the other. Neither position is inside, but
  the path between them is.
- **Why only the moment of contact counts:** `touching && !wasTouching`. Holding the blade inside
  the dummy is not repeated attacks; pulling out and swinging again is. The rules' 0.6 s cooldown
  is a second guard against one swing counting twice.
- **Why it forgets the previous tip when tracking drops:** when the sword re-appears somewhere
  else, the jump from old to new position is not a swing and must not count as one.
- **Why it only sends an `ActionMessage`:** the detector says *an attack happened*; the engine side
  decides *hit or block*. It stands in for the IMU, so it lives on **OfflineEngine** and is
  disabled online. If the team decides the camera should judge contact, it becomes an online reporter.

Select **OfflineEngine** in the Hierarchy to see the capsule drawn in red in the Scene view.

---

## 8. Your sword's trail (`AR/SwordTrail.cs`)

The dummy's trails are switched on by events baked into its animation clips. A real prop has no
clips, so your sword's trail is driven by **speed** instead: it starts when the rendered tip moves
faster than 1.2 m/s and stops below 0.6 m/s. Two thresholds rather than one, so a swing hovering
around a single speed doesn't flicker the trail on and off. It uses the same `BladeTrail` as the
dummy, drawn between the sword's `trailBase` and `trailTip` anchors.

---

## 9. Limitations (measured on the iPhone 13 Pro)

| Limitation | Cause | Mitigation |
|---|---|---|
| Lost beyond roughly 45–50° of tilt | The marker becomes too foreshortened to match | Keep the flat of the blade facing the phone |
| Lost during very fast swings | Motion blur removes the image detail ARKit matches | 0.3 s hold; swept-path hit check; swing at moderate speed |
| Needs the marker flat and uncovered | Bends and fingers change or hide the image | Stiff blade, 3–4 cm gap between hand and marker |
| Every contact is a "horizontal swing" | A camera can't classify the move reliably | The IMU classifier does this in the full system |
| Real objects don't hide virtual ones | No occlusion: virtual content is always drawn on top of the camera image | Accepted for this demo. ARKit's LiDAR depth could add occlusion later |

Virtual content never affects tracking: ARKit works on the raw camera image, before anything is
drawn over it. Even when the rendered sword covers the marker on screen, tracking is unaffected.

---

## 10. Debug overlay and buttons

**`AR/TrackedImageDebug.cs`** draws an outline on every tracked marker, the same size as the print,
with an arrow to the top of the picture: **green** = tracking, **yellow** = limited. The readout in
the bottom-left shows each marker's state and distance.

**`UI/DebugPanel.cs`**, bottom-right:

| Button | Calls |
|---|---|
| RESET | sends `START_MATCH_REQUEST` outbound |
| MOVE DUMMY | `DummyPlacer.BeginPlacement` |
| TRACKING: ON/OFF | toggles `TrackedImageDebug.Visible`; tracking itself carries on |

