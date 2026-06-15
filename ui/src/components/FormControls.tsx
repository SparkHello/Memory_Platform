import { normalizeDecimalInput, normalizeDecimalInputOnBlur } from "../utils/gateway";
import { displayText, valueText } from "../utils/format";

export function FilterSelect({
  label,
  value,
  options,
  onChange
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="field-block">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option} value={option}>
            {displayText(option)}
          </option>
        ))}
      </select>
    </label>
  );
}

export function DecimalInput({
  value,
  onChange,
  placeholder,
  step = "0.000001",
  emptyValueOnBlur = "0"
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  step?: string;
  emptyValueOnBlur?: string;
}) {
  return (
    <input
      inputMode="decimal"
      step={step}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(normalizeDecimalInput(event.target.value))}
      onBlur={(event) => onChange(normalizeDecimalInputOnBlur(event.target.value, emptyValueOnBlur))}
    />
  );
}

export function RangeFields({
  label,
  min,
  max,
  step,
  from,
  to,
  onChange
}: {
  label: string;
  min: number;
  max: number;
  step: number;
  from: number;
  to: number;
  onChange: (from: number, to: number) => void;
}) {
  return (
    <div className="range-field">
      <span>{label}</span>
      <div>
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          value={from}
          onChange={(event) => onChange(Number(event.target.value), to)}
        />
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          value={to}
          onChange={(event) => onChange(from, Number(event.target.value))}
        />
      </div>
    </div>
  );
}

export function FieldList({
  entries,
  compact
}: {
  entries: Array<[string, unknown]>;
  compact?: boolean;
}) {
  return (
    <dl className={`field-list ${compact ? "compact" : ""}`}>
      {entries.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{valueText(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
