import { displayTextForMode } from "../utils/format";

export function Badge({ value, expertMode = true }: { value: string; expertMode?: boolean }) {
  return <span className={`badge badge-${value}`}>{displayTextForMode(value, expertMode)}</span>;
}

export function badge(value: string, expertMode = true) {
  return <Badge value={value} expertMode={expertMode} />;
}
