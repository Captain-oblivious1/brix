/**
 * Loads the N-API addon (`rust/brix-node`, the Node twin of brix-py). The
 * built `.node` ships under `native/`; in the dev tree we also accept the
 * cargo target directly. Override with `BRIX_NODE_ADDON`.
 */

import * as fs from "fs";
import * as path from "path";

export interface NativeBinding {
  /** Async (returns a Promise): heavy build runs off the JS loop, events live. */
  build(
    specJson: string,
    configJson: string,
    onEvent?: (json: string) => void,
  ): Promise<string>;
  status(specJson: string, configJson: string): string;
  clean(specJson: string, configJson: string): string;
}

function load(): NativeBinding {
  const override = process.env.BRIX_NODE_ADDON;
  const candidates = override
    ? [override]
    : [
        path.join(__dirname, "..", "native", "brix_node.node"),
        path.join(__dirname, "..", "..", "rust", "target", "release", "libbrix_node.so"),
        path.join(__dirname, "..", "..", "rust", "target", "debug", "libbrix_node.so"),
      ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      // A load error on a file that exists is real — let it surface.
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      return require(candidate) as NativeBinding;
    }
  }
  throw new Error(
    `brix native addon not found (looked in: ${candidates.join(", ")}); ` +
      "build it with `npm run build:native`",
  );
}

export const native: NativeBinding = load();
