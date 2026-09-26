# Progressive discovery increment

> Current production source policy: [REAL_DATA.md](REAL_DATA.md). Earlier simulator/demo descriptions below apply only to retained test fixtures; the product generates no observations.

Issue #16 follows backend integration PR #15 and the primary [dashboard specification](SPEC.md).
A new simulated map starts with zero observed points and unknown occupancy.
Each simulated scan contributes a bounded batch of previously unseen samples within the five-meter sensor range and illustrative viewing angle.
Furniture occludes samples behind it; discovered geometry persists when the camera turns away.
Only observed objects enter the visible inventory and event history.
Tracking loss pauses discovery, and session reset clears discovery memory.
The hidden room model remains simulator input and is never rendered as an already known room.
The relocation action is a scripted, labeled simulator revisit and turns its sensor toward the changed object.
Remove the separate inventory tab while preserving searchable objects beside the scene.

This is an interactive demonstration of incremental spatial memory, not a calibrated LiDAR or navigation-physics model.
Use [SENSOR_PROFILE.md](SENSOR_PROFILE.md) for the range source and remaining camera calibration boundary.

Remove the top brand bar and group Simulation, Stop Rover, Export Snapshot, and New Session in the workspace heading.
Keep the group accessible on scroll and at narrow widths.

## Full-screen spatial workspace

The spatial view occupies the viewport by default.
Spatial Memory, Object Intelligence, Recent Activity, and Rover Controls remain available as expandable panels over the map.
Source selection, Stop, export, and new-session actions remain inside the view.
The default controls expose Standard and Explore.
Standard offers arrow-key steering and click-to-navigate together; the transport handles the backend's separate manual/navigate commands without exposing separate mutually exclusive UI modes.
Arrow-key directions are rover-relative: Up moves straight ahead immediately, Down turns around, and Left/Right turn 90° before advancing. Capture the requested heading at the start of each new direction press, independent of camera orbit or 2D/3D view. Holding or repeating a key must not accumulate turns.
Steering rotates by the shortest angle before applying forward velocity; the opposite direction is a 180-degree turn, never an automatic reverse.
Releasing a key, losing focus, opening an input/dialog, resetting the map, or pressing Stop cancels steering and pending handoffs.
Only an already explicitly armed, healthy Standard session may change between manual and navigation input without another arm gesture.
Any fault or Stop requires explicit arming again.

## Minimal canvas

The default canvas shows only a compact 2D/3D switch, Standard/Explore modes, and a smaller Arm control that becomes an always-available Stop while armed or arming.
Remove permanent source/actions bars, telemetry, statistics, legends, view toolbox, panel dock and bottom footer from the canvas.
Keep rover geometry, observed surfaces, object labels, view scope, trail, navigation and orbit/pan/zoom available.
Transient notices and the unavailable-renderer fallback remain visible when needed.

Preserve Spatial Memory, Object Intelligence, Recent Activity, full Rover Controls, settings, export, new session, view layers and diagnostics inside an on-demand workspace drawer.
Open it with Escape, the context-menu key or Shift+F10, a stationary right-click, or a stationary touch long-press; right-drag must rotate without opening it.
Mouse mappings are fixed: left-drag pans and right-drag rotates; middle-drag orbits, Shift + middle-drag pans, and stationary left-click retains navigation.
A keyboard-focus-only Open workspace button provides a discoverable accessible entry without persistent visual clutter.
Opening or closing the drawer cancels held steering; panel/dialog controls must not steer the rover.
Retain the simulation/source distinction in Arm labels and workspace details, and keep existing safe command/API behavior.

## Deferred auditory guidance concept

Parked for later agreement: conversational guidance for blind users using Gemini, live phone pose, deterministic route computation and fresh depth evidence.
The earlier seated auditory-room exploration concept is also unimplemented.
Current work remains rendering quality; this note does not authorize a voice-guidance implementation or claim walking/navigation safety.
