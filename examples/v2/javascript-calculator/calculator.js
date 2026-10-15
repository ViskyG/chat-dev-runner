export function calculate(left, operation, right) {
  if (!Number.isFinite(left) || !Number.isFinite(right)) throw new Error('Operands must be finite numbers');
  switch (operation) {
    case '+': return left + right;
    case '-': return left - right;
    case '*': return left * right;
    case '/': if (right === 0) throw new Error('Cannot divide by zero'); return left / right;
    default: throw new Error('Unknown operation');
  }
}
