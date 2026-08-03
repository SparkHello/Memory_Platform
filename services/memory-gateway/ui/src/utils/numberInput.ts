export function normalizeDecimalInput(raw: string): string {
  const value = raw.trim().replace(",", ".");
  if (!value) {
    return "";
  }
  const clean = value.replace(/[^\d.]/g, "");
  if (!clean) {
    return "";
  }
  const dotIndex = clean.indexOf(".");
  const hasDecimal = dotIndex !== -1;
  const wholeRaw = hasDecimal ? clean.slice(0, dotIndex) : clean;
  const fraction = hasDecimal ? clean.slice(dotIndex + 1).replace(/\./g, "") : "";
  const whole = wholeRaw.replace(/^0+(?=\d)/, "") || "0";
  return hasDecimal ? `${whole}.${fraction}` : whole;
}

export function normalizeDecimalInputOnBlur(raw: string, emptyValue = "0"): string {
  const normalized = normalizeDecimalInput(raw);
  if (!normalized) {
    return emptyValue;
  }
  if (normalized === "0.") {
    return "0";
  }
  if (normalized.endsWith(".")) {
    return normalized.slice(0, -1) || "0";
  }
  return normalized;
}
