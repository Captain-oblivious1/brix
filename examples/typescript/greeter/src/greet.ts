// The greeter library module. `greet` is the public API the app depends on;
// its body is implementation. Change only the body (e.g. the punctuation) and
// tsc re-emits a byte-identical greet.d.ts, so the app *replays* rather than
// recompiling — the ts-dts lens at work. Add or change an *exported* signature
// and the app recompiles.
export function greet(name: string): string {
    return "Hello, " + name + "!";
}
