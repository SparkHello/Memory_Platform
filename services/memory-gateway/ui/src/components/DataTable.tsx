import type { ReactNode } from "react";

export function DataTable({ children }: { children: ReactNode }) {
  return (
    <div className="table-wrap">
      <table className="data-table">{children}</table>
    </div>
  );
}
