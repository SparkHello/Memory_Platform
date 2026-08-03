import { Eye, EyeOff } from "lucide-react";
import { useState } from "react";

export function SecretInput({
  value,
  onChange,
  placeholder
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="secret-field">
      <input
        type={visible ? "text" : "password"}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
      />
      <button
        className="icon-button"
        type="button"
        onClick={() => setVisible((current) => !current)}
        title={visible ? "隐藏" : "显示"}
      >
        {visible ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </div>
  );
}
