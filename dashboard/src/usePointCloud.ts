import { useEffect, useState, useSyncExternalStore } from "react";
import { PointCloudStore } from "./pointCloud";

export function liveEndpoint(pageURL: string): string | null {
  const page = new URL(pageURL);
  const requested = page.searchParams.get("live");
  if (requested === "off") return null;
  const endpoint = requested ? new URL(requested) : new URL(page.origin);
  if (!requested) {
    endpoint.protocol = page.protocol === "https:" ? "wss:" : "ws:";
    endpoint.port = "8765";
    endpoint.pathname = "/live";
  }
  if (
    !["ws:", "wss:"].includes(endpoint.protocol) ||
    endpoint.username ||
    endpoint.password ||
    endpoint.hash
  )
    throw Error("Invalid point feed URL");
  return endpoint.href;
}

export function usePointCloud() {
  const [cloud] = useState(() => new PointCloudStore());
  useSyncExternalStore(cloud.subscribe, cloud.snapshot);
  const [feed, setFeed] = useState({
    connection: "connecting",
    phone: "unknown",
    tracking: "unknown",
    lastPoint: 0,
    rejected: 0,
  });
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let endpoint: string | null;
    try {
      endpoint = liveEndpoint(window.location.href);
    } catch {
      setFeed((s) => ({ ...s, connection: "invalid" }));
      return;
    }
    if (!endpoint) {
      setFeed((s) => ({ ...s, connection: "disabled" }));
      return;
    }
    let disposed = false,
      attempt = 0,
      socket: WebSocket | undefined;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const connect = () => {
      if (disposed) return;
      setFeed((s) => ({ ...s, connection: "connecting" }));
      try {
        socket = new WebSocket(endpoint);
      } catch {
        setFeed((s) => ({ ...s, connection: "invalid" }));
        return;
      }
      const current = socket;
      current.onopen = () => {
        if (disposed || current !== socket) return;
        attempt = 0;
        cloud.clear();
        setFeed({
          connection: "connected",
          phone: "unknown",
          tracking: "unknown",
          lastPoint: 0,
          rejected: 0,
        });
      };
      current.onmessage = (event) => {
        if (disposed || current !== socket || typeof event.data !== "string")
          return;
        if (event.data.length > 512_000) {
          setFeed((s) => ({ ...s, rejected: s.rejected + 1 }));
          return;
        }
        let message;
        try {
          message = JSON.parse(event.data);
        } catch {
          setFeed((s) => ({ ...s, rejected: s.rejected + 1 }));
          return;
        }
        if (!message || message.version !== 1) return;
        if (message.type === "points") {
          const result = cloud.ingest(message);
          if (result === "accepted")
            setFeed((s) => ({ ...s, lastPoint: Date.now() }));
          if (result === "invalid")
            setFeed((s) => ({ ...s, rejected: s.rejected + 1 }));
        } else if (message.type === "objects") {
          if (cloud.announce(message)) setFeed((s) => ({ ...s, lastPoint: 0 }));
        } else if (
          message.type === "health" &&
          ["ok", "stale", "down"].includes(message.phone)
        ) {
          setFeed((s) => ({ ...s, phone: message.phone }));
        } else if (
          message.type === "pose" &&
          ["normal", "limited", "not_available"].includes(message.tracking)
        ) {
          setFeed((s) => ({ ...s, tracking: message.tracking }));
        }
      };
      current.onerror = () => current.close();
      current.onclose = () => {
        if (disposed || current !== socket) return;
        setFeed((s) => ({ ...s, connection: "offline" }));
        retry = setTimeout(connect, Math.min(8000, 1000 * 2 ** attempt++));
      };
    };
    connect();
    return () => {
      disposed = true;
      clearTimeout(retry);
      if (socket) {
        socket.onclose = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.close();
      }
    };
  }, [cloud]);

  const live =
    feed.connection === "connected" &&
    feed.lastPoint > 0 &&
    now - feed.lastPoint < 2500 &&
    feed.phone !== "down" &&
    feed.phone !== "stale" &&
    feed.tracking !== "limited" &&
    feed.tracking !== "not_available";
  let label = live ? "Live RGB + depth" : "Waiting for RGB + depth";
  if (feed.connection === "connecting") label = "Connecting to point feed";
  if (feed.connection === "offline") label = "Feed disconnected";
  if (feed.connection === "invalid") label = "Invalid point feed URL";
  if (feed.connection === "disabled") label = "Point feed off";
  if (feed.connection === "connected" && cloud.count && !live)
    label = "No fresh depth";
  if (feed.connection === "connected" && feed.tracking === "limited")
    label = "Tracking limited";
  if (feed.connection === "connected" && feed.tracking === "not_available")
    label = "Tracking unavailable";
  if (feed.connection === "connected" && feed.phone === "down")
    label = "Phone offline";
  return { cloud, live, label, rejected: feed.rejected };
}
