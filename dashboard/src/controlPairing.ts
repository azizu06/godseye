import type { ConnectionConfig } from "./transport";

const slot = "godseye.control-pairing.v1";

/** Remember explicit pairing for this tab only, bound to both backend addresses. */
export function restoreControlPairing(
  config: ConnectionConfig,
  storage: Pick<Storage, "getItem">,
): ConnectionConfig {
  try {
    const saved = JSON.parse(storage.getItem(slot) ?? "null");
    if (
      saved?.apiUrl === config.apiUrl &&
      saved?.wsUrl === config.wsUrl &&
      saved?.commands === true &&
      typeof saved.roverKey === "string" &&
      saved.roverKey.length > 0
    )
      return { ...config, commands: true, roverKey: saved.roverKey };
  } catch {
    /* Unavailable or malformed storage keeps controls off. */
  }
  return config;
}

export function rememberControlPairing(
  config: ConnectionConfig,
  storage: Pick<Storage, "setItem" | "removeItem">,
) {
  try {
    if (config.commands && config.roverKey) {
      storage.setItem(
        slot,
        JSON.stringify({
          apiUrl: config.apiUrl,
          wsUrl: config.wsUrl,
          commands: true,
          roverKey: config.roverKey,
        }),
      );
    } else storage.removeItem(slot);
  } catch {
    /* Private browser settings may prevent session storage. */
  }
}
