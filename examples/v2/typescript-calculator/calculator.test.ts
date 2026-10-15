import { describe, expect, it } from 'vitest';
import { calculate, type Operation } from './calculator';
describe('calculator', () => {
  it.each<[number, Operation, number, number]>([[7,'+',5,12],[7,'-',5,2],[7,'*',5,35],[7,'/',2,3.5]])('%s %s %s = %s', (left, op, right, expected) => {
    expect(calculate(left, op, right)).toBe(expected);
  });
  it('rejects division by zero', () => expect(() => calculate(1, '/', 0)).toThrow());
  it('rejects non-finite input', () => expect(() => calculate(NaN, '+', 1)).toThrow());
});
