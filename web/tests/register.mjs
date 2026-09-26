// Lets `node --test --experimental-strip-types` import the application's modules as written:
// extensionless relative imports and the `@/` alias, both of which the Next.js bundler resolves
// and plain Node does not.
import { register } from "node:module";

register("./resolve-ts.mjs", import.meta.url);
