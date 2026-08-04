// Planted violations for the TypeScript pipeline. Misspelled coment here too.
export function recieveTotal(items: number[]): number {
  const unusedThing = 42; // unused variable -> eslint no-unused-vars
  let total = 0;
  for (const item of items) {
    total += item;
  }
  return total;
}

export function runUserCode(src: string): unknown {
  return eval(src); // eval -> eslint security plugin
}
