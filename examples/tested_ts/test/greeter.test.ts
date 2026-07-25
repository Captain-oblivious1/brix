import { expect, test } from 'vitest';
import { greet } from '../src/greeter';
test('greets', () => expect(greet('World')).toBe('Hello, World!'));
