import { describe, expect, it } from 'vitest';
import { calculate } from './calculator.js';
describe('calculator', () => {
  it.each([[7,'+',5,12],[7,'-',5,2],[7,'*',5,35],[7,'/',2,3.5]])('%s %s %s = %s', (left, op, right, expected) => {
    expect(calculate(left, op, right)).toBe(expected);
  });
  it.each([[1,'/',0],[NaN,'+',1],[1,'?',2]])('rejects invalid input %s %s %s', (left, op, right) => {
    expect(() => calculate(left, op, right)).toThrow();
  });
});
