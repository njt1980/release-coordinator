import { useEffect, useState } from "react";

// Column layout: callers left, centre services in the middle, called services right.
const COLUMN_WIDTH = 190;
const NODE_WIDTH = 150;
const NODE_HEIGHT = 34;
const ROW_GAP = 14;
const TOP = 34;
const SIDE = 34; // room for loops around the outer columns

function columnTitle(column, centerLabel) {
  if (column === 0) return centerLabel;
  const hops = Math.abs(column);
  if (column < 0) return hops === 1 ? "Direct callers" : `Callers, ${hops} hops`;
  return hops === 1 ? "Calls" : `Called, ${hops} hops`;
}

function truncate(text, limit = 21) {
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

/** Draws an ego graph returned by the API (nodes with a column, edges between them). */
export function EgoGraphDrawing({ graph, centerLabel = "Service" }) {
  const columns = [...new Set(graph.nodes.map((n) => n.column))].sort((a, b) => a - b);
  const byColumn = Object.fromEntries(columns.map((c) => [c, graph.nodes.filter((n) => n.column === c)]));
  const tallest = Math.max(...columns.map((c) => byColumn[c].length));
  const width = SIDE * 2 + columns.length * COLUMN_WIDTH - (COLUMN_WIDTH - NODE_WIDTH);
  const height = TOP + tallest * (NODE_HEIGHT + ROW_GAP) + SIDE;
  const position = {};
  const columnOf = Object.fromEntries(graph.nodes.map((n) => [n.id, n.column]));
  columns.forEach((column, columnIndex) => {
    const nodes = byColumn[column];
    const offset = ((tallest - nodes.length) * (NODE_HEIGHT + ROW_GAP)) / 2;
    nodes.forEach((node, row) => {
      position[node.id] = {
        x: SIDE + columnIndex * COLUMN_WIDTH,
        y: TOP + offset + row * (NODE_HEIGHT + ROW_GAP),
      };
    });
  });

  const path = (edge) => {
    const from = position[edge.source];
    const to = position[edge.target];
    if (!from || !to) return null;
    const y1 = from.y + NODE_HEIGHT / 2;
    const y2 = to.y + NODE_HEIGHT / 2;
    if (to.x > from.x) {
      const x1 = from.x + NODE_WIDTH;
      const x2 = to.x;
      const mid = (x1 + x2) / 2;
      return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2 - 6},${y2}`;
    }
    if (to.x < from.x) {
      // Backwards (a cycle): from the source's left side to the target's right side.
      const x1 = from.x;
      const x2 = to.x + NODE_WIDTH;
      const mid = (x1 + x2) / 2;
      return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2 + 6},${y2}`;
    }
    // Same column: loop around the column's outer side, away from the centre.
    const outer = edge.column < 0;
    const x = outer ? from.x : from.x + NODE_WIDTH;
    const bulge = outer ? x - 26 : x + 26;
    return `M${x},${y1} C${bulge},${y1} ${bulge},${y2} ${outer ? x - 6 : x + 6},${y2}`;
  };

  return (
    <svg
      role="img"
      aria-label={`Dependency map: ${graph.nodes.length} services and endpoints, ${graph.edges.length} dependencies`}
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className="max-w-none"
    >
      <defs>
        <marker id="ego-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" className="fill-slate-400" />
        </marker>
      </defs>
      {columns.map((column, index) => (
        <text
          key={column}
          x={SIDE + index * COLUMN_WIDTH + NODE_WIDTH / 2}
          y={16}
          textAnchor="middle"
          className="fill-slate-500 text-[11px] font-semibold uppercase tracking-wide"
        >
          {columnTitle(column, centerLabel)}
        </text>
      ))}
      {graph.edges.map((edge) => {
        const d = path({ ...edge, column: columnOf[edge.source] });
        return d ? (
          <path
            key={`${edge.source}>${edge.target}`}
            d={d}
            fill="none"
            markerEnd="url(#ego-arrow)"
            strokeWidth={edge.runtime ? 1.6 : 1.2}
            strokeDasharray={edge.runtime ? undefined : "4 3"}
            className={edge.runtime ? "stroke-slate-500" : "stroke-slate-400"}
          >
            <title>{`${edge.source.split(":").pop()} → ${edge.target.split(":").pop()} (${edge.methods.join(", ")})`}</title>
          </path>
        ) : null;
      })}
      {graph.nodes.map((node) => {
        const { x, y } = position[node.id];
        const style = node.center
          ? "fill-brand-50 stroke-brand-500"
          : node.kind === "unresolved"
            ? "fill-amber-50 stroke-amber-400"
            : "fill-white stroke-slate-300";
        return (
          <g key={node.id}>
            <title>{node.kind === "unresolved" ? `${node.label} (unresolved: no single service)` : `${node.namespace}/${node.label}`}</title>
            <rect
              x={x}
              y={y}
              width={NODE_WIDTH}
              height={NODE_HEIGHT}
              rx={8}
              strokeWidth={node.center ? 2 : 1}
              strokeDasharray={node.kind === "unresolved" ? "3 2" : undefined}
              className={style}
            />
            <text x={x + NODE_WIDTH / 2} y={y + NODE_HEIGHT / 2 + 4} textAnchor="middle" className="fill-slate-800 text-[12px]">
              {truncate(node.label)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** Fetches an ego graph from ``url`` and draws it with a legend. */
export default function EgoGraph({ url, centerLabel }) {
  const [graph, setGraph] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setGraph(null);
    setError(null);
    fetch(url)
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail ?? `Request failed with status ${response.status}`);
        return body;
      })
      .then((data) => !cancelled && setGraph(data))
      .catch((caught) => !cancelled && setError(caught.message));
    return () => {
      cancelled = true;
    };
  }, [url]);

  if (error) return <p className="text-sm text-slate-500">{error}</p>;
  if (!graph) return <p className="text-sm text-slate-500">Loading the dependency map…</p>;
  return (
    <div>
      <div className="overflow-x-auto rounded-xl bg-white p-2 ring-1 ring-slate-200">
        <EgoGraphDrawing graph={graph} centerLabel={centerLabel} />
      </div>
      <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>Arrows point from caller to called.</span>
        <span>Solid: seen in telemetry. Dashed: code or configuration only.</span>
        <span>Amber dashed box: an endpoint that matches no single service.</span>
        {graph.left_out > 0 && <span>{graph.left_out} farther node(s) not drawn.</span>}
      </p>
    </div>
  );
}
