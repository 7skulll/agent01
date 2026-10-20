export function percent(value: number | null | undefined, fallback = '未评分'): string {
  return value == null ? fallback : `${(value * 100).toFixed(1)}%`
}
