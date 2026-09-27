import { requestRoute, type RouteResult } from "./approachRoute";
export interface ReconJob {
  id: string;
  key: string;
  apiUrl: string;
  body: Parameters<typeof requestRoute>[1];
}
export interface ReconResult {
  key: string;
  result: RouteResult;
}
/** FIFO admission covers every person. Refreshes replace queued work without moving it ahead of others. */
export class ReconPlanner {
  private desired = new Map<string, ReconJob>();
  private queue: string[] = [];
  private active = new Map<string, { key: string; abort: AbortController }>();
  private done = new Map<string, string>();
  private disposed = false;
  constructor(
    private publish: (id: string, result: ReconResult) => void,
    private request = requestRoute,
  ) {}
  reconcile(jobs: ReconJob[]) {
    if (this.disposed) return;
    this.desired = new Map(jobs.map((j) => [j.id, j]));
    for (const id of this.done.keys())
      if (!this.desired.has(id)) this.done.delete(id);
    for (const [id, run] of this.active)
      if (this.desired.get(id)?.key !== run.key) run.abort.abort();
    this.queue = this.queue.filter((id) => this.desired.has(id));
    for (const job of jobs)
      if (
        this.done.get(job.id) !== job.key &&
        (this.active.get(job.id)?.key !== job.key ||
          this.active.get(job.id)?.abort.signal.aborted) &&
        !this.queue.includes(job.id)
      )
        this.queue.push(job.id);
    this.drain();
  }
  private drain() {
    if (this.disposed) return;
    while (this.active.size < 2) {
      const index = this.queue.findIndex((id) => !this.active.has(id));
      if (index < 0) return;
      const id = this.queue.splice(index, 1)[0],
        job = this.desired.get(id);
      if (!job) continue;
      const abort = new AbortController();
      this.active.set(id, { key: job.key, abort });
      let timer: ReturnType<typeof setTimeout>;
      let timedOut = false;
      const timeout = new Promise<RouteResult>((resolve) => {
        timer = setTimeout(() => {
          timedOut = !abort.signal.aborted;
          abort.abort();
          resolve({
            status: "unavailable",
            reason: "route_service_unavailable",
          });
        }, 10000);
      });
      Promise.race([this.request(job.apiUrl, job.body, abort.signal), timeout])
        .catch((): RouteResult => ({
          status: "unavailable",
          reason: "route_service_unavailable",
        }))
        .then((result) => {
          if (
            !this.disposed &&
            this.desired.get(id)?.key === job.key &&
            (!abort.signal.aborted || timedOut)
          ) {
            this.done.set(id, job.key);
            this.publish(id, { key: job.key, result });
          }
        })
        .finally(() => {
          clearTimeout(timer);
          this.active.delete(id);
          this.drain();
        });
    }
  }
  dispose() {
    this.disposed = true;
    this.queue = [];
    this.desired.clear();
    for (const run of this.active.values()) run.abort.abort();
  }
}
