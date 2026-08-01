import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { DDLPreview, PreviewRelationship } from "../api/types";

interface LineageCanvasProps {
  preview: DDLPreview | null;
  highlightedIds: string[];
}

interface DrawnRelationship {
  relationship: PreviewRelationship;
  path: string;
}

export function LineageCanvas({ preview, highlightedIds }: LineageCanvasProps) {
  const innerRef = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<DrawnRelationship[]>([]);
  const [focusedTable, setFocusedTable] = useState<string | null>(null);
  const highlighted = useMemo(() => new Set(highlightedIds), [highlightedIds]);

  const drawRelationships = useCallback(() => {
    const inner = innerRef.current;
    if (!preview || !inner) {
      setPaths([]);
      return;
    }
    const innerRect = inner.getBoundingClientRect();
    const drawn = preview.relationships.flatMap((relationship) => {
      const source = inner.querySelector<HTMLElement>(
        `[data-column-id="${CSS.escape(relationship.source_column_id)}"]`,
      );
      const target = inner.querySelector<HTMLElement>(
        `[data-column-id="${CSS.escape(relationship.target_column_id)}"]`,
      );
      if (!source || !target) return [];
      const from = source.getBoundingClientRect();
      const to = target.getBoundingClientRect();
      const x1 = from.right - innerRect.left;
      const y1 = from.top + from.height / 2 - innerRect.top;
      const x2 = to.left - innerRect.left;
      const y2 = to.top + to.height / 2 - innerRect.top;
      const bend = Math.max(44, Math.abs(x2 - x1) * 0.42);
      const direction = x2 >= x1 ? 1 : -1;
      return [{
        relationship,
        path: `M ${x1} ${y1} C ${x1 + bend * direction} ${y1}, ${x2 - bend * direction} ${y2}, ${x2} ${y2}`,
      }];
    });
    setPaths(drawn);
  }, [preview]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(drawRelationships);
    window.addEventListener("resize", drawRelationships);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", drawRelationships);
    };
  }, [drawRelationships]);

  const tableById = new Map(preview?.tables.map((table) => [table.id, table]) ?? []);
  const columnById = new Map(
    preview?.tables.flatMap((table) => table.columns.map((column) => [column.id, column] as const)) ?? [],
  );

  const relationshipLabel = (relationship: PreviewRelationship) => {
    const sourceTable = tableById.get(relationship.source_table_id);
    const targetTable = tableById.get(relationship.target_table_id);
    const sourceColumn = columnById.get(relationship.source_column_id);
    const targetColumn = columnById.get(relationship.target_column_id);
    return `${sourceTable?.qualified_name ?? "外部表"}.${sourceColumn?.name ?? "字段"} → ${targetTable?.qualified_name ?? relationship.target_table_name}.${targetColumn?.name ?? relationship.target_column_name}`;
  };

  return (
    <section className="lineage-panel" aria-labelledby="canvas-title">
      <header className="canvas-toolbar">
        <div><span>LIVE LINEAGE</span><h2 id="canvas-title">Schema Canvas</h2></div>
        <p>{preview ? `${preview.table_count} tables · ${preview.column_count} columns · ${preview.relationships.length} foreign keys` : "结构由确定性 parser 生成"}</p>
      </header>
      <div className="lineage-viewport" tabIndex={0} aria-label="Schema 关系画布，可横向和纵向滚动">
        <div ref={innerRef} className="lineage-inner">
          <svg className="relationship-layer" aria-hidden="true">
            {paths.map(({ relationship, path }) => (
              <path
                key={`${relationship.source_column_id}:${relationship.target_column_id}`}
                d={path}
                className={`relationship-path ${focusedTable === relationship.source_table_id || focusedTable === relationship.target_table_id ? "is-focused" : ""}`}
              />
            ))}
          </svg>
          <div className="schema-nodes">
            {preview?.tables.map((table) => {
              const related = preview.relationships.filter(
                (relationship) => relationship.source_table_id === table.id || relationship.target_table_id === table.id,
              );
              const isHighlighted = highlighted.has(table.id)
                || table.columns.some((column) => highlighted.has(column.id));
              return (
                <article
                  id={`table-${table.id}`}
                  key={table.id}
                  className={`schema-node ${isHighlighted ? "is-related" : ""}`}
                  tabIndex={0}
                  aria-label={`${table.qualified_name}，${table.columns.length} 个字段，${related.length} 条外键关系`}
                  onFocus={() => setFocusedTable(table.id)}
                  onBlur={() => setFocusedTable(null)}
                >
                  <header className="node-head">
                    <strong>{table.qualified_name}</strong>
                    <span>{table.columns.length} COLS</span>
                  </header>
                  <ul className="column-list">
                    {table.columns.map((column) => (
                      <li key={column.id} data-column-id={column.id} className="column-row">
                        <span className="key-role">
                          {column.structural_role === "primary_key" ? "PK" : column.structural_role === "foreign_key" ? "FK" : ""}
                        </span>
                        <span className="column-name">{column.name}</span>
                        <span className="column-type">{column.data_type}{column.nullable ? " ?" : ""}</span>
                      </li>
                    ))}
                  </ul>
                  {related.length > 0 && <p className="relation-note">{related.map(relationshipLabel).join(" · ")}</p>}
                </article>
              );
            })}
          </div>
          {!preview?.tables.length && (
            <div className="canvas-empty">
              <span aria-hidden="true">⌁</span>
              <strong>载入 DDL，建立真实结构画布</strong>
              <p>只显示 parser 识别的表、字段与外键，不推测关系。</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
