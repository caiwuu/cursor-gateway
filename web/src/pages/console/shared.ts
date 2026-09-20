export const USER_ORIGIN = import.meta.env.DEV ? "http://127.0.0.1:8788" : window.location.origin;

export function maskToken(value: string) {
  if (!value) return "";
  if (value.length <= 16) return value;
  return `${value.slice(0, 7)}…${value.slice(-4)}`;
}

export function ledgerKind(kind: string) {
  if (kind === "recharge") return "充值";
  if (kind === "adjust") return "调账";
  if (kind === "redeem") return "兑换";
  if (kind === "usage") return "消费";
  return kind || "其他";
}
