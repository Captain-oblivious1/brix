// The app module. It imports the greeter's *built* declaration
// (../../greeter/dist/greet.d.ts, which tsc resolves on disk) and calls into it.
// brix tracks that declaration through the ts-dts lens, so the app rebuilds only
// when the greeter's public API changes.
import { greet } from "../../greeter/dist/greet";

console.log(greet("brix"));
