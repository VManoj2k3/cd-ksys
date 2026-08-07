/** Labeled semantic bugs for the LLM layer — deterministic layers stay quiet.
 *  Used by tests/accuracy_eval.py to measure LLM recall on TypeScript. */

export function average(values: number[]): number {
  let total = 0;
  for (const value of values) {
    total += value;
  }
  return total / values.length; // BUG line 9: NaN for empty input (no guard)
}

export function sumItems(items: number[]): number {
  let total = 0;
  for (let i = 0; i <= items.length; i++) { // BUG line 14: off by one -> NaN
    total += items[i];
  }
  return total;
}

export function dropZeros(numbers: number[]): number[] {
  for (let i = 0; i < numbers.length; i++) {
    if (numbers[i] === 0) {
      numbers.splice(i, 1); // BUG line 23: skips the element after each removal
    }
  }
  return numbers;
}

export function firstUpper(users: string[]): string {
  return users[0].toUpperCase(); // BUG line 30: TypeError when users is empty
}
