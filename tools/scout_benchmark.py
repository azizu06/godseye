"""Deterministic closed-loop Scout benchmark. SYNTHETIC, never hardware evidence.

Run ``python -m tools.scout_benchmark --output-dir /tmp/scout-benchmark``.
Use ``--revision HEAD`` to compare checked-in navigation/prototype code with local
edits using exactly the same synthetic response. No sockets, hardware or providers.
The real follower and prototype packet conversion run every simulated 0.1 s,
with real replanning every 4 s. This isolates driving behavior; it does not simulate sensing, asynchronous
Navigator scheduling, transport leases or physical motor calibration.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import types

import numpy as np

from backend import navigation, navigator, prototype


@dataclass(frozen=True)
class Pose:
    x: float
    z: float
    yaw: float


@dataclass(frozen=True)
class Footprint:
    # Benchmark fixtures only, not defaults for a physical rover or calibration.
    length_m: float = .30
    width_m: float = .20
    margin_m: float = .08

    def __post_init__(self):
        if (not all(math.isfinite(v) for v in asdict(self).values()) or
                min(self.length_m, self.width_m) <= 0 or self.margin_m < 0):
            raise ValueError('synthetic footprint dimensions must be positive and margin nonnegative')

    @property
    def radius_m(self):
        return math.hypot(self.length_m, self.width_m) / 2


@dataclass(frozen=True)
class SyntheticResponse:
    """Invented linear wheel response at PWM180; no physical calibration claim.

    D1 1/2 counter-rotate wheels, 3 drives both forward, 5/6 drive one wheel
    at half PWM and the other at full PWM, as in BridgeCore's packet mapping.
    Positive yaw follows backend convention (toward +X from +Z). Track width is
    the effective turning parameter, independent of the collision-body fixture.
    """
    wheel_speed_mps: float = .30
    track_width_m: float = .28

    def __post_init__(self):
        if (not all(math.isfinite(v) for v in asdict(self).values()) or
                self.wheel_speed_mps < 0 or self.track_width_m <= 0):
            raise ValueError('synthetic wheel speed must be nonnegative and track width positive')

    def rates(self, packet):
        if packet is None:
            return 0., 0.
        if packet.direction not in (1, 2, 3, 5, 6) or not 0 < packet.pwm <= 180:
            raise ValueError('unsupported prototype motor packet')
        wheel = self.wheel_speed_mps * packet.pwm / 180
        if packet.direction in (1, 2):
            return 0., (1 if packet.direction == 1 else -1) * 2 * wheel / self.track_width_m
        if packet.direction == 3:
            return wheel, 0.
        inner = self.wheel_speed_mps * (packet.pwm // 2) / 180
        return (wheel + inner) / 2, (1 if packet.direction == 5 else -1) * (wheel - inner) / self.track_width_m


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    grid: navigation.Grid
    start: Pose
    goal: tuple[float, float]
    max_time_s: float = 120.


def integrate(pose: Pose, speed: float, yaw_rate: float, dt: float) -> Pose:
    """Exact constant-twist integration in the backend's (sin yaw, cos yaw) frame."""
    end_yaw = pose.yaw + yaw_rate * dt
    if abs(yaw_rate) < 1e-12:
        dx, dz = speed * dt * math.sin(pose.yaw), speed * dt * math.cos(pose.yaw)
    else:
        dx = speed / yaw_rate * (math.cos(pose.yaw) - math.cos(end_yaw))
        dz = speed / yaw_rate * (math.sin(end_yaw) - math.sin(pose.yaw))
    return Pose(pose.x + dx, pose.z + dz, math.remainder(end_yaw, math.tau))


def collides(grid, pose: Pose, body: Footprint) -> bool:
    """Oriented physical chassis vs closed unsafe cell squares, including off-grid.

    The planner's extra margin is intentionally excluded from collision geometry;
    a margin violation is not mislabeled as physical contact.
    """
    sx, cz = math.sin(pose.yaw), math.cos(pose.yaw)
    half_l, half_w = body.length_m / 2, body.width_m / 2
    rx, rz = abs(sx) * half_l + abs(cz) * half_w, abs(cz) * half_l + abs(sx) * half_w
    ox, oz = grid.origin
    if (pose.x - rx <= ox or pose.z - rz <= oz or
            pose.x + rx >= ox + grid.width * grid.cell_m or
            pose.z + rz >= oz + grid.height * grid.cell_m):
        return True
    r0, c0 = grid.cell_of(pose.x - rx - 1e-12, pose.z - rz - 1e-12)
    r1, c1 = grid.cell_of(pose.x + rx, pose.z + rz)
    rows, cols = np.nonzero(grid.cells[r0:r1 + 1, c0:c1 + 1] != navigation.FREE)
    if not len(rows):
        return False
    dx = ox + (cols + c0 + .5) * grid.cell_m - pose.x
    dz = oz + (rows + r0 + .5) * grid.cell_m - pose.z
    half_cell = grid.cell_m / 2
    # Separating-axis theorem for an oriented rectangle and axis-aligned squares.
    overlap = (np.abs(dx) <= rx + half_cell) & (np.abs(dz) <= rz + half_cell)
    cell_projection = half_cell * (abs(sx) + abs(cz))
    overlap &= np.abs(dx * sx + dz * cz) <= half_l + cell_projection
    overlap &= np.abs(dx * cz - dz * sx) <= half_w + cell_projection
    return bool(overlap.any())


def make_scenarios() -> list[Scenario]:
    def room(width, height, openings=None, obstacles=()):
        cell = .05
        cols, rows = round(width / cell), round(height / cell)
        x, z = np.meshgrid((np.arange(cols) + .5) * cell, (np.arange(rows) + .5) * cell)
        regions = openings or ((.15, .15, width - .15, height - .15),)
        free = np.zeros((rows, cols), dtype=bool)
        for x0, z0, x1, z1 in regions:
            free |= (x >= x0) & (x < x1) & (z >= z0) & (z < z1)
        for x0, z0, x1, z1 in obstacles:
            free &= ~((x >= x0) & (x < x1) & (z >= z0) & (z < z1))
        return navigation.Grid.from_array(np.where(free, 1, 2), origin=(0., 0.), cell_m=cell)

    return [
        Scenario('off_center_corridor', 'Off-center corridor', room(2.4, 6.4),
                 Pose(.62, .7, -.18), (1.2, 5.6)),
        Scenario('obstacle_detour', 'Center obstacle detour',
                 room(3.2, 6.4, obstacles=((1.3, 2.6, 1.9, 3.2),)),
                 Pose(1.6, .7, 0.), (1.6, 5.6)),
        Scenario('right_angle_corner', 'Right-angle corridor',
                 room(5.2, 5.2, openings=((.2, .2, 1.8, 5.), (1.8, 3.4, 5., 5.))),
                 Pose(1., .7, 0.), (4.4, 4.2)),
        Scenario('goal_approach', 'Close goal approach', room(2.4, 2.4),
                 Pose(1.2, .7, 0.), (1.2, 1.4), max_time_s=30.),
    ]


def _stack(revision=None):
    """Optional historical sources for fair before/after runs; no checkout mutation."""
    if revision is None:
        modules = (navigation, prototype, navigator)
        sources = [Path(module.__file__).read_text() for module in modules]
        return *modules, dict(zip(('navigation', 'prototype', 'navigator'),
                                 (hashlib.sha256(s.encode()).hexdigest() for s in sources)))
    root = Path(__file__).resolve().parents[1]
    sources = [subprocess.run(['git', 'show', f'{revision}:backend/{name}.py'], cwd=root,
                              check=True, capture_output=True, text=True).stdout
               for name in ('navigation', 'prototype', 'navigator')]
    nav = types.ModuleType('_scout_benchmark_navigation')
    sys.modules[nav.__name__] = nav  # dataclass annotation resolution during exec
    exec(compile(sources[0], f'{revision}:backend/navigation.py', 'exec'), nav.__dict__)
    proto = types.ModuleType('_scout_benchmark_prototype')
    nav_run = types.ModuleType('_scout_benchmark_navigator')
    sys.modules[nav_run.__name__] = nav_run
    original = sys.modules['backend.navigation']
    try:
        sys.modules['backend.navigation'] = nav
        exec(compile(sources[1], f'{revision}:backend/prototype.py', 'exec'), proto.__dict__)
        exec(compile(sources[2], f'{revision}:backend/navigator.py', 'exec'), nav_run.__dict__)
    finally:
        sys.modules['backend.navigation'] = original
    return nav, proto, nav_run, dict(zip(('navigation', 'prototype', 'navigator'),
                               (hashlib.sha256(s.encode()).hexdigest() for s in sources)))


def run_scenario(scenario: Scenario, *, response=None, body=None, response_mode='prototype',
                 dt=.1, replan_s=4., stack=None):
    response, body = response or SyntheticResponse(), body or Footprint()
    if response_mode not in ('prototype', 'ideal'):
        raise ValueError('response mode must be prototype or ideal')
    if not math.isfinite(dt) or dt <= 0 or not math.isfinite(replan_s) or replan_s <= 0:
        raise ValueError('tick and replan periods must be positive')
    nav, proto, nav_run = (stack or (navigation, prototype, navigator))[:3]
    adapter = proto.PrototypeActuation()
    config = replace(nav_run.NavSettings().planner, robot_radius_m=body.radius_m, margin_m=body.margin_m,
                     unknown_traversable=False, footprint_clearance=True,
                     start_snap_radius_m=0., snap_radius_m=0.)
    pose, elapsed, distance = scenario.start, 0., 0.
    plans, trace, follower = [], [], None
    next_plan = 0.
    progress_pose, progress_time = pose, 0.
    reason, collided = 'timeout', False
    pivot_time, moving_time = 0., 0.

    def record(requested=(0., 0.), actual=(0., 0.), packet=None, status='stop', target=None):
        trace.append(dict(t=round(elapsed, 6), x=round(pose.x, 8), z=round(pose.z, 8),
                          yaw=round(pose.yaw, 8), requested=list(requested), actual=list(actual),
                          packet=[packet.direction, packet.pwm] if packet else None,
                          status=status, target=list(target) if target else None))

    while elapsed < scenario.max_time_s - 1e-9:
        if collides(scenario.grid, pose, body):
            reason, collided = 'collision', True
            break
        if elapsed >= next_plan - 1e-9:
            plan = nav.plan_path(scenario.grid, (pose.x, pose.z), scenario.goal, config)
            plans.append(dict(t=round(elapsed, 6), points=plan.points, reason=plan.reason))
            next_plan = elapsed + replan_s
            if not plan.ok:
                reason = plan.reason
                break
            follower = follower.replaced(plan.points) if follower else nav.PurePursuit(plan.points, adapter.follower())
        command = follower.step(pose.x, pose.z, pose.yaw)
        if command.arrived:
            reason = 'arrived'
            break
        requested = (command.v_mps, command.yaw_rate_rps)
        packet = adapter.command(*requested)
        actual = response.rates(packet) if response_mode == 'prototype' else requested
        record(requested, actual, packet, command.status, command.target)
        tick = min(dt, scenario.max_time_s - elapsed)
        # Sample at <=1 cm chassis-corner travel so a 0.1 s step cannot leap a wall.
        steps = max(1, math.ceil((abs(actual[0]) + abs(actual[1]) * body.radius_m) * tick / .01))
        for _ in range(steps):
            previous = pose
            pose = integrate(pose, *actual, tick / steps)
            distance += math.hypot(pose.x - previous.x, pose.z - previous.z)
            elapsed += tick / steps
            moving_time += tick / steps if actual[0] else 0.
            pivot_time += tick / steps if actual[1] and not actual[0] else 0.
            if collides(scenario.grid, pose, body):
                reason, collided = 'collision', True
                break
        if collided:
            break
        if (math.hypot(pose.x - progress_pose.x, pose.z - progress_pose.z) >= .05 or
                abs(math.remainder(pose.yaw - progress_pose.yaw, math.tau)) >= .15):
            progress_pose, progress_time = pose, elapsed
        elif elapsed - progress_time >= 5. - 1e-9:
            reason = 'no_progress'
            break
    record(status=reason)
    steering = [{1: 1, 2: -1, 5: 1, 6: -1}.get(p['packet'][0], 0)
                if p['packet'] else 0 for p in trace[:-1]]
    packets = [p['packet'] for p in trace[:-1]]
    metrics = dict(completed=reason == 'arrived', collision=collided, stop_reason=reason,
                   elapsed_s=round(elapsed, 6), distance_m=round(distance, 6),
                   final_goal_distance_m=round(math.hypot(pose.x - scenario.goal[0], pose.z - scenario.goal[1]), 6),
                   steering_switches=sum(a != b for a, b in zip(steering, steering[1:])),
                   packet_switches=sum(a != b for a, b in zip(packets, packets[1:])),
                   pivot_time_s=round(pivot_time, 6), moving_time_s=round(moving_time, 6),
                   replans=max(0, len(plans) - 1))
    return dict(name=scenario.name, title=scenario.title, start=asdict(scenario.start),
                goal=list(scenario.goal), metrics=metrics, plans=plans, trace=trace,
                planner_config=asdict(config), follower_config=asdict(adapter.follower()),
                grid=dict(origin=list(scenario.grid.origin), cell_m=scenario.grid.cell_m,
                          cells=scenario.grid.cells.tolist()))


def run_suite(*, names=None, response=None, body=None, response_mode='prototype', revision=None):
    response, body = response or SyntheticResponse(), body or Footprint()
    stack = _stack(revision)
    scenarios = make_scenarios()
    if names:
        unknown = set(names) - {s.name for s in scenarios}
        if unknown:
            raise ValueError(f'unknown scenarios: {sorted(unknown)}')
        scenarios = [s for s in scenarios if s.name in names]
    results = [run_scenario(s, response=response, body=body, response_mode=response_mode, stack=stack)
               for s in scenarios]
    return dict(schema_version=1, evidence='SYNTHETIC ONLY — not hardware evidence',
                scope='Real planner/follower/prototype packets; ideal sensing and instantaneous synthetic wheel response. '
                      'No Navigator scheduling, transport, feedback latency or hardware validation.',
                source_revision=revision or 'working_tree', source_sha256=stack[3],
                response_mode=response_mode, synthetic_response=asdict(response),
                synthetic_footprint=asdict(body), dt_s=.1, replan_s=4.,
                metric_definitions=dict(steering_switches='Changes between left, straight, right or idle motor-packet categories; terminal stop excluded.',
                                        packet_switches='Direction or PWM changes; terminal stop excluded.',
                                        collision='Oriented chassis touches an unsafe cell or map boundary; planner margin excluded.'),
                metrics=dict(completed=sum(r['metrics']['completed'] for r in results),
                             collisions=sum(r['metrics']['collision'] for r in results), total=len(results)),
                scenarios=results)


_HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scout · Synthetic driving replay</title><style>
*{box-sizing:border-box}body{margin:0;background:#101820;color:#e9f0ee;font:15px system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:28px}h1{font-size:30px;margin:8px 0}p{color:#a8bbb9;line-height:1.5}.badge{color:#ffd087;font-size:12px;letter-spacing:.08em}header{margin-bottom:24px}.layout{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:20px}.panel{border:1px solid #304348;border-radius:12px;background:#15242b;padding:18px}canvas{display:block;width:100%;height:570px;background:#0d191f;border-radius:8px}button,select,input{font:inherit}button,select{background:#243b43;color:#eef7f5;border:1px solid #4b656b;border-radius:7px;padding:8px 12px}button{cursor:pointer}.controls{display:flex;align-items:center;gap:12px;margin:16px 0}input{width:100%}table{width:100%;border-collapse:collapse;font-size:13px}td{padding:9px 0;border-bottom:1px solid #304348}td:last-child{text-align:right}#status{color:#65dec3;font-weight:650}pre{white-space:pre-wrap;color:#a8bbb9;font-size:12px;line-height:1.5}.legend{font-size:12px;color:#a8bbb9}.legend b{margin-right:16px}.route{color:#8f99a7}.trail{color:#65dec3}.target{color:#e8c26a}@media(max-width:800px){.layout{grid-template-columns:1fr}canvas{height:440px}}
</style><main><header><span class="badge">SYNTHETIC ONLY · NO HARDWARE EVIDENCE</span><h1>Scout driving laboratory</h1><p>Real planning and steering code. Invented motor response. Follow every command through a deterministic course.</p></header>
<div class="controls"><select id="scenario" aria-label="Scenario"></select><button id="play">Play</button><select id="speed" aria-label="Replay speed"><option value="1">1×</option><option value="4" selected>4×</option><option value="12">12×</option></select><span id="clock"></span></div>
<div class="layout"><div class="panel"><canvas id="map" aria-label="Overhead route replay"></canvas><input id="scrub" type="range" min="0" value="0" aria-label="Replay time"><div class="legend"><b class="route">— Planned path</b><b class="trail">— Driven trail</b><b class="target">● Pursuit target</b></div></div>
<aside class="panel"><p id="status"></p><table id="metrics"></table><h3>Current command</h3><pre id="command"></pre><h3>Model assumptions</h3><pre id="model"></pre></aside></div><p id="scope"></p></main>
<script id="benchmark-data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('benchmark-data').textContent),byId=id=>document.getElementById(id);
const select=byId('scenario'),canvas=byId('map'),ctx=canvas.getContext('2d'),scrub=byId('scrub');let run,index=0,playing=false,last=0,playTime=0;
data.scenarios.forEach((s,i)=>{const o=document.createElement('option');o.value=i;o.textContent=s.title;select.append(o)});
byId('scope').textContent=data.scope;byId('model').textContent=JSON.stringify({mode:data.response_mode,...data.synthetic_response,footprint:data.synthetic_footprint,source:data.source_revision},null,2);
function choose(){run=data.scenarios[+select.value];index=0;playTime=0;scrub.max=run.trace.length-1;const m=run.metrics;byId('status').textContent=m.completed?'Arrived':m.stop_reason.replaceAll('_',' ');byId('status').style.color=m.completed?'#65dec3':'#ffb98f';byId('metrics').replaceChildren();[['Completion',m.completed?'Yes':'No'],['Collision',m.collision?'Yes':'No'],['Time',m.elapsed_s.toFixed(1)+' s'],['Travel',m.distance_m.toFixed(2)+' m'],['Goal distance',m.final_goal_distance_m.toFixed(2)+' m'],['Steering switches',m.steering_switches],['Packet switches',m.packet_switches],['Pivot time',m.pivot_time_s.toFixed(1)+' s'],['Replans',m.replans]].forEach(([label,value])=>{const tr=document.createElement('tr');for(const v of [label,value]){const td=document.createElement('td');td.textContent=v;tr.append(td)}byId('metrics').append(tr)});draw()}
function draw(){const box=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1;canvas.width=box.width*dpr;canvas.height=box.height*dpr;ctx.scale(dpr,dpr);const w=box.width,h=box.height,g=run.grid,rows=g.cells.length,cols=g.cells[0].length,scale=Math.min((w-50)/(cols*g.cell_m),(h-50)/(rows*g.cell_m)),left=(w-cols*g.cell_m*scale)/2,top=(h-rows*g.cell_m*scale)/2;
const point=(x,z)=>[left+(x-g.origin[0])*scale,h-top-(z-g.origin[1])*scale];
g.cells.forEach((row,r)=>row.forEach((v,c)=>{ctx.fillStyle=v===2?'#475c64':v===1?'#1d323a':'#101820';ctx.fillRect(left+c*g.cell_m*scale,h-top-(r+1)*g.cell_m*scale,g.cell_m*scale+.3,g.cell_m*scale+.3)}));
function line(points,color,width=2){if(!points.length)return;ctx.beginPath();points.forEach(([x,z],i)=>{const p=point(x,z);i?ctx.lineTo(...p):ctx.moveTo(...p)});ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke()}
const frame=run.trace[index],plan=run.plans.filter(p=>p.t<=frame.t).at(-1);if(plan)line(plan.points,'#8896a8');line(run.trace.slice(0,index+1).map(p=>[p.x,p.z]),'#65dec3',3);
function dot(xz,color,r){ctx.beginPath();ctx.arc(...point(...xz),r,0,Math.PI*2);ctx.fillStyle=color;ctx.fill()}dot(run.goal,'#eee6c5',6);if(frame.target)dot(frame.target,'#e8c26a',4);
const body=data.synthetic_footprint,corners=[[-1,-1],[1,-1],[1,1],[-1,1]].map(([a,b])=>[frame.x+a*body.length_m/2*Math.sin(frame.yaw)+b*body.width_m/2*Math.cos(frame.yaw),frame.z+a*body.length_m/2*Math.cos(frame.yaw)-b*body.width_m/2*Math.sin(frame.yaw)]);line([...corners,corners[0]],'#80edda',2);line([[frame.x,frame.z],[frame.x+body.length_m*Math.sin(frame.yaw),frame.z+body.length_m*Math.cos(frame.yaw)]],'#fff',2);
byId('clock').textContent=frame.t.toFixed(1)+' / '+run.metrics.elapsed_s.toFixed(1)+' s';scrub.value=index;byId('command').textContent='State: '+frame.status+'\nPacket: '+(frame.packet?'D1='+frame.packet[0]+' PWM='+frame.packet[1]:'STOP')+'\nRequested: '+frame.requested.map(n=>n.toFixed(3)).join(', ')+'\nSynthetic: '+frame.actual.map(n=>n.toFixed(3)).join(', ')+'\n           m/s, rad/s';}
select.onchange=choose;scrub.oninput=()=>{index=+scrub.value;playTime=run.trace[index].t;draw()};byId('play').onclick=()=>{if(index===run.trace.length-1){index=0;playTime=0}playing=!playing;byId('play').textContent=playing?'Pause':'Play'};window.onresize=draw;
function tick(now){if(playing){playTime+=(now-last)/1000*+byId('speed').value;while(index<run.trace.length-1&&run.trace[index+1].t<=playTime)index++;draw();if(index===run.trace.length-1){playing=false;byId('play').textContent='Play'}}last=now;requestAnimationFrame(tick)}choose();requestAnimationFrame(tick);
</script></html>'''


def save_report(report, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    json_path, html_path = output_dir / 'benchmark.json', output_dir / 'replay.html'
    json_path.write_text(raw + '\n')
    html_path.write_text(_HTML.replace('__DATA__', raw.replace('<', '\\u003c')))
    return json_path, html_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--scenario', action='append', choices=[s.name for s in make_scenarios()])
    parser.add_argument('--response-mode', choices=('prototype', 'ideal'), default='prototype')
    parser.add_argument('--synthetic-wheel-speed-mps', type=float, default=.30, help='Invented wheel speed at PWM180')
    parser.add_argument('--synthetic-track-width-m', type=float, default=.28)
    parser.add_argument('--revision', help='Load navigation/prototype source from a Git revision, e.g. HEAD')
    args = parser.parse_args()
    report = run_suite(names=args.scenario, response_mode=args.response_mode, revision=args.revision,
                       response=SyntheticResponse(args.synthetic_wheel_speed_mps, args.synthetic_track_width_m))
    print(report['evidence'])
    for result in report['scenarios']:
        m = result['metrics']
        print(f"{result['name']:24} {m['stop_reason']:14} {m['elapsed_s']:6.1f}s "
              f"{m['distance_m']:5.2f}m switches={m['steering_switches']:3} collision={m['collision']}")
    if args.output_dir:
        for path in save_report(report, args.output_dir):
            print(path.resolve())
    # Outcomes are data, not an assertion that every existing controller can finish.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
