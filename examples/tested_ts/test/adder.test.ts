import { expect, test } from 'vitest';
import { add } from '../src/adder';
test('adds', () => expect(add(2, 3)).toBe(5));
