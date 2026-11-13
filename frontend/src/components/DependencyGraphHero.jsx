import { useEffect, useState } from "react";

// A generic shop, not any customer's (or the lab's) real topology.
const NODES = {
  storefront: { x: 78, y: 86, label: "storefront" },
  admin: { x: 78, y: 304, label: "admin" },
  gateway: { x: 232, y: 195, label: "gateway" },
  orders: { x: 392, y: 104, label: "orders" },
  inventory: { x: 392, y: 286, label: "inventory" },
  payments: { x: 552, y: 56, label: "payments" },
  queue: { x: 552, y: 195, label: "queue" },
  db: { x: 552, y: 334, label: "db" },
};

const EDGES = [
  ["storefront", "gateway"],
  ["admin", "gateway"],
  ["gateway", "orders"],
  ["gateway", "inventory"],
  ["orders", "payments"],
  ["orders", "queue"],
  ["inventory", "queue"],
  ["inventory", "db"],
];

const CHANGED = "orders";
const UPSTREAM = new Set(["gateway", "storefront", "admin"]);
const DOWNSTREAM = new Set(["payments", "queue"]);
const IN_RADIUS = new Set([CHANGED, ...UPSTREAM, ...DOWNSTREAM]);
const RADIUS_EDGES = new Set([
  "storefront>gateway",
  "admin>gateway",
  "gateway>orders",
  "orders>payments",
  "orders>queue",
]);

export const PHASES = [
  "Live traffic flows between eight services.",
  "A pull request changes orders.",
  "The baseline gives its radius: three callers, two dependencies.",
  "Live telemetry confirms every edge in the radius.",
  "Verdict: held until there is contract evidence.",
];

const PHASE_MS = 2600;
const NODE_W = 92;
const NODE_H = 32;

const edgePath = (from, to) => {
  const a = NODES[from];
  const b = NODES[to];
  const x1 = a.x + NODE_W / 2;
  const x2 = b.x - NODE_W / 2;
  const mid = (x1 + x2) / 2;
  return `M ${x1} ${a.y} C ${mid} ${a.y}, ${mid} ${b.y}, ${x2} ${b.y}`;
};

function usePrefersReducedMotion() {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(
    () => typeof window !== "undefined" && window.matchMedia?.(query).matches,
  );
  useEffect(() => {
    const media = window.matchMedia?.(query);
    if (!media) return undefined;
    const onChange = () => setReduced(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

/** Cycles through PHASES; holds the last one when motion is reduced. */
export function useHeroPhase() {
  const reduced = usePrefersReducedMotion();
  const [phase, setPhase] = useState(0);
  useEffect(() => {
    if (reduced) {
      setPhase(PHASES.length - 1);
      return undefined;
    }
    const timer = setInterval(
      () => setPhase((current) => (current + 1) % PHASES.length),
      PHASE_MS,
    );
    return () => clearInterval(timer);
  }, [reduced]);
  return phase;
}

function edgeStyle(key, phase) {
  const inRadius = RADIUS_EDGES.has(key);
  if (phase >= 3 && inRadius) return { stroke: "#2dd4bf", opacity: 1, width: 2.2 };
  if (phase >= 2) return inRadius
    ? { stroke: "#a5b4fc", opacity: 0.95, width: 2 }
    : { stroke: "#4a5683", opacity: 0.35, width: 1.5 };
  return { stroke: "#7c89b8", opacity: 0.7, width: 1.6 };
}

function nodeStyle(id, phase) {
  if (id === CHANGED && phase >= 1) {
    return { fill: "#4f46e5", stroke: "#a5b4fc", text: "#ffffff" };
  }
  if (phase >= 2 && IN_RADIUS.has(id)) {
    return { fill: "#1b2445", stroke: phase >= 3 ? "#2dd4bf" : "#818cf8", text: "#ffffff" };
  }
  if (phase >= 2) return { fill: "#111832", stroke: "#2a3560", text: "#6b77a3" };
  return { fill: "#1b2445", stroke: "#3b4775", text: "#cdd5ee" };
}

function Tag({ x, y, children, tone }) {
  const colors =
    tone === "up"
      ? { fill: "#312e81", text: "#c7d2fe" }
      : { fill: "#134e4a", text: "#99f6e4" };
  return (
    <g className="animate-fade-up">
      <rect x={x - 34} y={y - 9} width="68" height="18" rx="9" fill={colors.fill} />
      <text x={x} y={y + 3.5} textAnchor="middle" fontSize="9.5" fontWeight="600" fill={colors.text}>
        {children}
      </text>
    </g>
  );
}

export default function DependencyGraphHero({ phase }) {
  return (
    <svg
      viewBox="0 0 640 400"
      className="h-auto w-full"
      role="img"
      aria-labelledby="hero-graph-title"
    >
      <title id="hero-graph-title">
        A service dependency graph. A change to the orders service highlights its
        callers and dependencies, live telemetry confirms those edges, and the
        release is held until contract evidence exists.
      </title>
      <defs>
        <pattern id="hero-grid" width="24" height="24" patternUnits="userSpaceOnUse">
          <path d="M 24 0 L 0 0 0 24" fill="none" stroke="#1b2445" strokeWidth="1" />
        </pattern>
        <radialGradient id="hero-glow" cx="60%" cy="30%" r="70%">
          <stop offset="0%" stopColor="#4f46e5" stopOpacity="0.28" />
          <stop offset="100%" stopColor="#0b1020" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect width="640" height="400" fill="url(#hero-grid)" />
      <rect width="640" height="400" fill="url(#hero-glow)" />

      {EDGES.map(([from, to]) => {
        const key = `${from}>${to}`;
        const style = edgeStyle(key, phase);
        const d = edgePath(from, to);
        return (
          <g key={key} style={{ transition: "opacity 600ms" }} opacity={style.opacity}>
            <path d={d} fill="none" stroke={style.stroke} strokeWidth={style.width} strokeOpacity="0.35" />
            <path
              d={d}
              fill="none"
              stroke={style.stroke}
              strokeWidth={style.width}
              strokeDasharray="4 8"
              strokeLinecap="round"
              className="animate-flow"
            />
          </g>
        );
      })}

      {Object.entries(NODES).map(([id, node]) => {
        const style = nodeStyle(id, phase);
        return (
          <g key={id}>
            {id === CHANGED && phase >= 1 && (
              <rect
                x={node.x - NODE_W / 2}
                y={node.y - NODE_H / 2}
                width={NODE_W}
                height={NODE_H}
                rx="10"
                fill="none"
                stroke="#818cf8"
                strokeWidth="2"
                className="origin-center-box animate-ring"
              />
            )}
            <rect
              x={node.x - NODE_W / 2}
              y={node.y - NODE_H / 2}
              width={NODE_W}
              height={NODE_H}
              rx="10"
              fill={style.fill}
              stroke={style.stroke}
              strokeWidth="1.5"
              style={{ transition: "fill 500ms, stroke 500ms" }}
            />
            <text
              x={node.x}
              y={node.y + 4}
              textAnchor="middle"
              fontSize="12"
              fontWeight="600"
              fill={style.text}
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
              style={{ transition: "fill 500ms" }}
            >
              {node.label}
            </text>
          </g>
        );
      })}

      {phase >= 2 && (
        <>
          {[...UPSTREAM].map((id) => (
            <Tag key={id} x={NODES[id].x} y={NODES[id].y - 28} tone="up">
              caller
            </Tag>
          ))}
          {[...DOWNSTREAM].map((id) => (
            <Tag key={id} x={NODES[id].x} y={NODES[id].y + 28} tone="down">
              dependency
            </Tag>
          ))}
        </>
      )}

      {phase >= 4 && (
        <g className="animate-fade-up">
          <rect x="212" y="356" width="216" height="30" rx="15" fill="#451a03" stroke="#f59e0b" strokeOpacity="0.6" />
          <circle cx="232" cy="371" r="4" fill="#f59e0b" />
          <text x="244" y="375" fontSize="11.5" fontWeight="600" fill="#fde68a">
            Held · COMPATIBILITY_UNVERIFIED
          </text>
        </g>
      )}
    </svg>
  );
}
