/**
 * Logical data expressions — the TS mirror of `src/brix/data.py`.
 *
 * User-facing declarations of *what data exists*: files, file collections,
 * tools, and literal values. Clean-build truth — nothing about caching,
 * staging, or incremental state. These are pure field-holders: the engine
 * (path matching, substitution, expansion) lives in the Rust core, so the TS
 * side only stores what it was given and knows how to enumerate its atoms.
 *
 * Language deviation from Python (TS has no operator overloading): Python's
 * `a + b` union is `a.plus(b)` here, and a glob's `g - "*_test"` narrowing is
 * `g.exclude("*_test")`. Same semantics, method form.
 */

export abstract class DataExpr {
  /** Union with another expression (Python's `+`). */
  plus(other: DataExpr): DataUnion {
    return new DataUnion(this, other);
  }

  /** The leaf atoms this expression contains, in order. */
  abstract atoms(): DataAtom[];
}

export abstract class DataAtom extends DataExpr {
  /**
   * The atom's comparison semantics: the name of the content lens
   * fingerprinting compares it through (e.g. `"jvm-abi"`). `undefined` — the
   * default for a plain file/glob — is byte hashing. Set only by a skin's typed
   * subclass (Stage 2), never a string the author types. See docs/content-key.md.
   */
  contentKey?: string;

  atoms(): DataAtom[] {
    return [this];
  }
}

export class FileData extends DataAtom {
  constructor(public readonly path: string) {
    super();
  }
}

export class GlobData extends DataAtom {
  readonly exclude: readonly string[];

  constructor(public readonly template: string, exclude: readonly string[] = []) {
    super();
    this.exclude = exclude;
  }

  /**
   * Narrow membership by one or more fnmatch patterns (Python's `glob - pat`).
   * Membership only — the planner's edge inference ignores excludes.
   */
  excluding(...patterns: string[]): GlobData {
    const g = new GlobData(this.template, [...this.exclude, ...patterns]);
    g.contentKey = this.contentKey;
    return g;
  }
}

export class RegexData extends DataAtom {
  constructor(public readonly pattern: string) {
    super();
  }
}

export class ToolData extends DataAtom {
  constructor(public readonly name: string) {
    super();
  }
}

export class ValueData extends DataAtom {
  constructor(public readonly text: string) {
    super();
  }
}

export class DataUnion extends DataExpr {
  readonly parts: readonly DataExpr[];

  constructor(...parts: DataExpr[]) {
    super();
    // Flatten nested unions, exactly like the Python constructor.
    const flat: DataExpr[] = [];
    for (const part of parts) {
      if (part instanceof DataUnion) flat.push(...part.parts);
      else flat.push(part);
    }
    this.parts = flat;
  }

  atoms(): DataAtom[] {
    return this.parts.flatMap((p) => p.atoms());
  }
}

/** Constructor namespace exposed as `graph.data` (and as the module `data`). */
export class DataFactory {
  file(path: string): FileData {
    return new FileData(path);
  }
  glob(template: string): GlobData {
    return new GlobData(template);
  }
  regex(pattern: string): RegexData {
    return new RegexData(pattern);
  }
  tool(name: string): ToolData {
    return new ToolData(name);
  }
  value(text: string): ValueData {
    return new ValueData(text);
  }
}

/** A shared factory, for authoring outside a Graph (mirrors `graph.data`). */
export const data = new DataFactory();
