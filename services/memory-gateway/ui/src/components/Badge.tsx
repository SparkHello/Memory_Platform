import { displayText } from "../utils/format";

export function Badge({ value }: { value: string }) {
  return <span className={`badge badge-${value}`}>{displayText(value)}</span>;
}

export function badge(value: string) {
  return <Badge value={value} />;
}
