// Synthetic CPU/worker-payload comparison, not physical-device or GPU FPS.
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";
const dashboard = resolve(fileURLToPath(new URL("..", import.meta.url)));
const root = resolve(dashboard, "..");
const ref = process.argv[2] ?? "ed940f8";
const directory = mkdtempSync(join(tmpdir(), "godseye-map-bench-"));
try {
  const baseline = execFileSync(
    "git",
    ["show", `${ref}:dashboard/src/persistentSurfaceMap.ts`],
    { cwd: root, encoding: "utf8" },
  ).replaceAll(
    '"./planarSurface"',
    JSON.stringify(join(dashboard, "src/planarSurface.ts")),
  );
  writeFileSync(join(directory, "baseline.ts"), baseline);
  writeFileSync(
    join(directory, "entry.ts"),
    `
import { PersistentSurfaceMap as Before } from ${JSON.stringify(join(directory, "baseline.ts"))};
import { PersistentSurfaceMap as After } from ${JSON.stringify(join(dashboard, "src/persistentSurfaceMap.ts"))};
const frames = Array.from({length:100},(_,frame)=>{
  const positions=new Float32Array(1200*9), colors=new Float32Array(1200*9).fill(.5), indices=new Uint32Array(1200*3);
  for(let i=0;i<1200;i++) {
    const x=(i%40)*.03, y=Math.floor(i/40)*.03, z=-1-frame*.01;
    positions.set([x,y,z,x+.01,y,z,x,y+.01,z],i*9);
    indices.set([i*3,i*3+1,i*3+2],i*3);
  }
  return {id:String(frame),positions,colors,indices};
});
const results=[], snapshots=[];
for(const [label,Class] of [[${JSON.stringify(ref)},Before],["integration",After]]) {
  const map=new Class(), times=[];let bytes=0;
  for(const patch of frames) {
    const start=performance.now(); map.add(patch);
    const data=label==='integration'?map.takeDelta():map.snapshot();
    bytes+=data.positions.byteLength+data.colors.byteLength+data.indices.byteLength;
    times.push(performance.now()-start);
  }
  times.sort((a,b)=>a-b); snapshots.push(map.snapshot());
  results.push({label,frames:frames.length,triangles:map.triangleCount,p50_ms:times[50],p95_ms:times[95],worker_bytes:bytes});
}
for(const key of ['positions','colors','indices']) {
  const [a,b]=snapshots.map(s=>s[key]);
  if(a.length!==b.length || !a.every((v,i)=>v===b[i])) throw Error('Geometry/color changed: '+key);
}
console.log(JSON.stringify({fixture:'100 batches of 1200 disconnected triangles, below capacity',identical_geometry_and_color:true,results},null,2));
`,
  );
  const bundled = join(directory, "run.mjs");
  buildSync({
    entryPoints: [join(directory, "entry.ts")],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: bundled,
  });
  process.stdout.write(
    execFileSync(process.execPath, [bundled], { encoding: "utf8" }),
  );
} finally {
  rmSync(directory, { recursive: true, force: true });
}
