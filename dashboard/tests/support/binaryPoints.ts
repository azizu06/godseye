export function binaryPoints(
  positions: number[],
  colors: number[],
  changes: Record<string, unknown> = {},
) {
  let header = JSON.stringify({
    version: 2,
    type: "points",
    chunk_id: 1,
    session_id: "dense-test",
    map_epoch: 1,
    frame_id: 1,
    t_capture: 1,
    count: positions.length / 3,
    positions: "float32_le",
    colors: "rgb8_srgb",
    ...changes,
  });
  header += " ".repeat((4 - (new TextEncoder().encode(header).length % 4)) % 4);
  const encoded = new TextEncoder().encode(header);
  const packet = new ArrayBuffer(
    4 + encoded.length + positions.length * 4 + colors.length,
  );
  new DataView(packet).setUint32(0, encoded.length, true);
  new Uint8Array(packet, 4, encoded.length).set(encoded);
  new Float32Array(packet, 4 + encoded.length, positions.length).set(positions);
  new Uint8Array(packet, 4 + encoded.length + positions.length * 4).set(colors);
  return packet;
}
