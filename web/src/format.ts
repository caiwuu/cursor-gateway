const UNITS = [
  { unit: 1e15, suffix: "P" },
  { unit: 1e12, suffix: "T" },
  { unit: 1e9, suffix: "B" },
  { unit: 1e6, suffix: "M" },
  { unit: 1e3, suffix: "K" },
] as const;

/** 小于该值仍用千分位，避免 1.2K 抢小数字的可读性。 */
const MIN_COMPACT = 10_000;

export function formatExact(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  if (!Number.isFinite(v)) return "0";
  return Math.round(v).toLocaleString();
}

/** 大数字动态切到 K / M / B / T / P，例如 2,772,855 → 2.77M。 */
export function formatCompact(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  if (!Number.isFinite(v)) return "0";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs < MIN_COMPACT) return sign + Math.round(abs).toLocaleString();
  for (const { unit, suffix } of UNITS) {
    if (abs >= unit) {
      const scaled = abs / unit;
      const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
      const text = scaled
        .toFixed(digits)
        .replace(/(\.\d*[1-9])0+$/, "$1")
        .replace(/\.0+$/, "");
      return `${sign}${text}${suffix}`;
    }
  }
  return sign + Math.round(abs).toLocaleString();
}

/** 余额按微元存储：1 元 = 1_000_000。 */
export function formatYuan(micros: number | null | undefined): string {
  const y = (Number(micros) || 0) / 1_000_000;
  if (!Number.isFinite(y)) return "¥0.00";
  return `¥${y.toFixed(4)}`;
}
