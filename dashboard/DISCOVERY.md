# Progressive discovery increment

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
