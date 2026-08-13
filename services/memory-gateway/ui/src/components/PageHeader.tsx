import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  subtitle,
  action,
  showTitle = true
}: {
  eyebrow?: string;
  title: string;
  subtitle?: string;
  action?: ReactNode;
  /** When the top bar already shows this page name, keep only the lede and actions. */
  showTitle?: boolean;
}) {
  return (
    <div className={`page-header${showTitle ? "" : " is-compact"}`}>
      <div>
        {eyebrow && <span className="page-eyebrow">{eyebrow}</span>}
        <h1 className={showTitle ? undefined : "sr-only"}>{title}</h1>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      {action && <div className="page-actions">{action}</div>}
    </div>
  );
}
