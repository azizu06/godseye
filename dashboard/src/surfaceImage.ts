/** Decode before publishing a patch so replacing a view never blanks its old texture. */
export function decodeSurfaceImage(
  jpeg: Uint8Array,
  signal: AbortSignal,
): Promise<HTMLCanvasElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(
      new Blob([new Uint8Array(jpeg)], { type: "image/jpeg" }),
    );
    const cleanup = () => {
      image.onload = null;
      image.onerror = null;
      signal.removeEventListener("abort", abort);
      URL.revokeObjectURL(url);
    };
    const abort = () => {
      cleanup();
      image.src = "";
      reject(new Error("Capture cancelled"));
    };
    image.onload = () => {
      cleanup();
      const scale = Math.min(
        1,
        1280 / Math.max(image.naturalWidth, image.naturalHeight),
      );
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
      canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
      const context = canvas.getContext("2d");
      if (!context) {
        reject(new Error("Color decoding unavailable"));
        return;
      }
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      resolve(canvas);
    };
    image.onerror = () => {
      cleanup();
      reject(new Error("Invalid color image"));
    };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    else image.src = url;
  });
}
