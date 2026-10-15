import { calculate } from './calculator.js';
const [left, operation, right] = process.argv.slice(2);
try {
  if (process.argv.length !== 5) throw new Error('Usage: node cli.js 12 / 4');
  console.log(calculate(Number(left), operation, Number(right)));
} catch (error) {
  console.error(error.message);
  process.exitCode = 2;
}
