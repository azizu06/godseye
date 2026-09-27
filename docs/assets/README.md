# README visuals

These assets belong to the root [README](../../README.md).

- `pip-hero.png`: generated concept art, created with the built-in image generation tool. It is an illustration of the project vision, not a hardware photograph or reconstruction result.
- `dashboard-3d.jpg` and `dashboard-2d.jpg`: unaltered browser screenshots of the actual dashboard connected to `python -m tools.sim_rover --dashboard` on local ports 5175/8775. Captured before the PIP rename, they retain the original God's Eye branding. The scene, detector inputs, and motor response are synthetic; the application processes them through its real mapping, localization, and rendering code. Voice providers are not enabled.
- `architecture.svg`: full-size Mermaid rendering of the diagram in the root README, exported with text labels for standalone viewing. The README's Mermaid block is the editable source; regenerate the SVG after changing it.

No physical-world performance should be inferred from these images. To reproduce the dashboard views, follow the simulator quick start in the root README, let observations accumulate, and switch between the 3D and 2D tabs. Exact geometry can vary with capture timing and the simulated route.

## Hero generation prompt

Built-in image generation was used with an opaque background and no reference images.
The original concept below was then edited with the built-in tool to use the final name, **PIP — Personal Intelligent Pathfinder**. The saved asset contains the final branding.

```text
Use case: stylized-concept
Asset type: cinematic wide GitHub README hero banner for the God's Eye robotics project.
Primary request: Make a spectacular, polished editorial sci-fi mission-control cover for a real hackathon project that mounts an iPhone with LiDAR on a small four-wheel educational rover, maps rooms in 3D, remembers objects, and answers spoken questions about the map. This is explicitly concept artwork, not a screenshot or proof of actual hardware performance.
Scene: A cutaway architectural room in a dark spatial void. Left portion mostly elegant negative space for title; right portion a real-looking small open acrylic educational rover with four rubber wheels, visible circuit boards and a mounted upright modern triple-camera iPhone whose rear cameras face into the room. Above and beyond it, the physical room transitions into a dense mint-green and pale cyan point-cloud reconstruction, thin isometric wireframe walls, and subtly boxed chair and bag objects. A restrained curved scan fan visually connects phone to map.
Style: Premium technical editorial, high-detail 3D illustration and blueprint overlay, beautiful atmospheric lighting, crisp geometry, believable small rover hardware, sophisticated warm white typography, graphite-black / deep petrol background with mint cyan accents and very small amber points. Cinematic, ambitious, not military.
Composition: panoramic 2.5:1 landscape banner, title on left, rover and reconstructed room on right, generous margins.
Text verbatim, large and beautifully typeset on left: "GOD'S EYE"
Smaller line below: "GIVE A ROOM A MEMORY."
Small bottom label: "SPATIAL INTELLIGENCE ON WHEELS"
Very small discreet bottom-right label: "CONCEPT ART"
Avoid: weapons, military symbols, scary surveillance eyes, faces, other brand logos, fake performance metrics, paragraphs, crowded HUD text, stock-art aesthetic, purple gradients. The four wheel rover must be a small DIY educational robot, never a car, tank, dog or humanoid. Do not simulate the real dashboard UI.
```

## Final branding edit prompt

```text
Correct one word in this PIP README hero banner. The expansion of PIP is "PERSONAL INTELLIGENT PATHFINDER", not "PERSONAL INDOOR PATHFINDER". Replace the small subtitle directly below the large PIP with exactly "PERSONAL INTELLIGENT PATHFINDER", keeping it readable and fitting within the left text area. Preserve every other element: the large PIP title, GIVE A ROOM A MEMORY., SPATIAL INTELLIGENCE ON WHEELS, CONCEPT ART, the rover and iPhone, room, point clouds, composition, panoramic dimensions, light and color. Change no other text.
```
