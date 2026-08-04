// Clean TypeScript — deterministic layers must stay silent.
export interface Reading {
  id: number;
  value: number;
}

const MAX_READINGS = 16;

export function totalOf(readings: Reading[]): number {
  return readings
    .slice(0, MAX_READINGS)
    .reduce((sum, reading) => sum + reading.value, 0);
}

export function describe(reading: Reading): string {
  return `sensor-${reading.id}: ${reading.value}`;
}
