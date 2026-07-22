import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type SimulationLinkDatum,
  type SimulationNodeDatum
} from "d3-force";
import { Minus, Plus, RotateCcw } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent
} from "react";
import type { MemoryNetwork as MemoryNetworkData, MemoryNetworkEdge, MemoryNetworkNode } from "../types";
import { MEMORY_TYPES } from "../utils/constants";
import { displayText } from "../utils/format";

type SimNode = MemoryNetworkNode &
  SimulationNodeDatum & {
    radius: number;
  };

type SimLink = Omit<MemoryNetworkEdge, "source" | "target"> &
  SimulationLinkDatum<SimNode> & {
    source: string | SimNode;
    target: string | SimNode;
  };

type ViewTransform = {
  scale: number;
  x: number;
  y: number;
};

type DragState = {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  origin: ViewTransform;
};

const WIDTH = 760;
const HEIGHT = 430;
const MIN_ZOOM = 0.58;
const MAX_ZOOM = 3.2;
const ZOOM_STEP = 1.18;
const INITIAL_VIEW: ViewTransform = { scale: 1, x: 0, y: 0 };

export function MemoryNetwork({
  network,
  selectedId,
  onSelect
}: {
  network: MemoryNetworkData;
  selectedId?: string | null;
  onSelect: (node: MemoryNetworkNode) => void;
}) {
  const graph = useMemo(() => buildGraph(network), [network]);
  const [view, setView] = useState<ViewTransform>(INITIAL_VIEW);
  const [isPanning, setIsPanning] = useState(false);
  const dragRef = useRef<DragState | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const frameRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setView(INITIAL_VIEW);
    dragRef.current = null;
    setIsPanning(false);
  }, [network]);

  const updateView = useCallback((next: ViewTransform | ((current: ViewTransform) => ViewTransform)) => {
    setView((current) => constrainView(typeof next === "function" ? next(current) : next));
  }, []);

  const zoomAt = useCallback(
    (origin: { x: number; y: number }, factor: number) => {
      updateView((current) => {
        const scale = clamp(current.scale * factor, MIN_ZOOM, MAX_ZOOM);
        const graphX = (origin.x - current.x) / current.scale;
        const graphY = (origin.y - current.y) / current.scale;
        return {
          scale,
          x: origin.x - graphX * scale,
          y: origin.y - graphY * scale
        };
      });
    },
    [updateView]
  );

  const zoomFromCenter = useCallback(
    (factor: number) => {
      zoomAt({ x: WIDTH / 2, y: HEIGHT / 2 }, factor);
    },
    [zoomAt]
  );

  const resetView = useCallback(() => {
    updateView(INITIAL_VIEW);
  }, [updateView]);

  // React 的 onWheel 是 passive 监听，preventDefault 无效会连带滚动页面；
  // 必须用原生非 passive 监听器接管滚轮缩放。挂在外层 frame 上以覆盖缩放
  // 工具栏区域，并以 graph 为依赖，保证空图→有图重渲染后监听仍在。
  useEffect(() => {
    const frame = frameRef.current;
    const svg = svgRef.current;
    if (!frame || !svg) return;
    const handleWheel = (event: WheelEvent) => {
      if ((event.target as Element | null)?.closest?.(".network-legend")) return;
      event.preventDefault();
      event.stopPropagation();
      const point = clientPointToSvg(svg, event.clientX, event.clientY);
      zoomAt(point, Math.exp(-event.deltaY * 0.0014));
    };
    frame.addEventListener("wheel", handleWheel, { passive: false });
    return () => frame.removeEventListener("wheel", handleWheel);
  }, [zoomAt, graph]);

  const handlePointerDown = useCallback((event: ReactPointerEvent<SVGSVGElement>) => {
    if (event.button !== 0) return;
    if ((event.target as Element).closest(".network-node")) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      origin: view
    };
    setIsPanning(true);
  }, [view]);

  const handlePointerMove = useCallback((event: ReactPointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const dx = ((event.clientX - drag.startClientX) / Math.max(1, rect.width)) * WIDTH;
    const dy = ((event.clientY - drag.startClientY) / Math.max(1, rect.height)) * HEIGHT;
    updateView({
      scale: drag.origin.scale,
      x: drag.origin.x + dx,
      y: drag.origin.y + dy
    });
  }, [updateView]);

  const finishPan = useCallback((event: ReactPointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setIsPanning(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }, []);

  if (!graph.nodes.length) {
    return <div className="state-block compact">还没有可展示的记忆网络</div>;
  }

  return (
    <div className="memory-network-frame" ref={frameRef}>
      <div className="network-zoom-toolbar" aria-label="网络视图控制">
        <button
          className="icon-button compact"
          type="button"
          title="缩小"
          aria-label="缩小"
          onClick={() => zoomFromCenter(1 / ZOOM_STEP)}
        >
          <Minus size={15} />
        </button>
        <button
          className="icon-button compact"
          type="button"
          title="放大"
          aria-label="放大"
          onClick={() => zoomFromCenter(ZOOM_STEP)}
        >
          <Plus size={15} />
        </button>
        <button
          className="icon-button compact"
          type="button"
          title="重置视图"
          aria-label="重置视图"
          onClick={resetView}
        >
          <RotateCcw size={15} />
        </button>
      </div>
      <svg
        ref={svgRef}
        className={`memory-network ${isPanning ? "is-panning" : ""}`}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label="记忆网络"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={finishPan}
        onPointerCancel={finishPan}
        onDoubleClick={resetView}
      >
        <g className="network-scene" transform={`translate(${view.x} ${view.y}) scale(${view.scale})`}>
          <g className="network-links">
            {graph.links.map((link) => {
              const source = link.source as SimNode;
              const target = link.target as SimNode;
              return (
                <line
                  key={link.id}
                  className={`network-link ${link.kind}`}
                  x1={source.x}
                  y1={source.y}
                  x2={target.x}
                  y2={target.y}
                  strokeWidth={link.kind === "core_evidence" ? 1.7 : 0.55 + link.weight * 0.85}
                />
              );
            })}
          </g>
          <g className="network-nodes">
            {graph.nodes.map((node) => {
              const selected = selectedId === node.id;
              return (
                <g
                  key={node.id}
                  className={`network-node ${node.kind} ${selected ? "selected" : ""}`}
                  transform={`translate(${node.x}, ${node.y})`}
                  onClick={() => onSelect(node)}
                  tabIndex={0}
                  role="button"
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(node);
                    }
                  }}
                >
                  <circle
                    r={node.radius}
                    fill={nodeFill(node)}
                    stroke={nodeStroke(node)}
                    strokeWidth={selected ? 2.6 : node.kind === "core" ? 1.8 : 1.05}
                  />
                  <title>{node.label}</title>
                  {(node.kind === "core" || selected) && (
                    <text y={node.radius + 14} textAnchor="middle">
                      {node.label}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
        </g>
      </svg>
      <div className="network-legend" aria-hidden="true">
        {MEMORY_TYPES.map((type) => (
          <span key={type}>
            <i style={{ background: TYPE_FILL[type] }} />
            {displayText(type)}
          </span>
        ))}
        <span>
          <i style={{ background: CORE_FILL, borderRadius: "50%" }} />
          核心
        </span>
      </div>
    </div>
  );
}

function buildGraph(network: MemoryNetworkData): { nodes: SimNode[]; links: SimLink[] } {
  const nodeCount = network.nodes.length;
  const memoryCount = network.nodes.filter((node) => node.kind === "memory").length;
  const density = clamp((nodeCount - 28) / 92, 0, 1);
  let coreIndex = 0;
  let memoryIndex = 0;
  const coreCount = Math.max(1, nodeCount - memoryCount);

  const nodes: SimNode[] = network.nodes.map((node) => {
    if (node.kind === "core") {
      const angle = (coreIndex / coreCount) * Math.PI * 2 - Math.PI / 2;
      coreIndex += 1;
      return {
        ...node,
        radius: nodeRadius(node),
        x: WIDTH / 2 + Math.cos(angle) * 66,
        y: HEIGHT / 2 + Math.sin(angle) * 52
      };
    }

    const index = memoryIndex;
    memoryIndex += 1;
    const layer = index % 3;
    const angle = index * 2.399963229728653 + layer * 0.18;
    const ring = clamp(138 + memoryCount * 1.15, 150, 270) - layer * (density > 0.55 ? 42 : 34);
    return {
      ...node,
      radius: nodeRadius(node),
      x: WIDTH / 2 + Math.cos(angle) * ring,
      y: HEIGHT / 2 + Math.sin(angle) * ring * 0.68
    };
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  const links: SimLink[] = network.edges
    .filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target))
    .map((edge) => ({ ...edge, source: edge.source, target: edge.target }));

  forceSimulation<SimNode>(nodes)
    .force(
      "link",
      forceLink<SimNode, SimLink>(links)
        .id((node) => node.id)
        .distance((link) => (link.kind === "core_evidence" ? 82 - density * 10 : 118 - density * 32))
        .strength((link) => (link.kind === "core_evidence" ? 0.48 : 0.14))
    )
    .force("charge", forceManyBody<SimNode>().strength((node) => (node.kind === "core" ? -260 : -95 - density * 48)))
    .force("center", forceCenter(WIDTH / 2, HEIGHT / 2))
    .force("x", forceX<SimNode>(WIDTH / 2).strength(0.025 + density * 0.02))
    .force("y", forceY<SimNode>(HEIGHT / 2).strength(0.035 + density * 0.025))
    .force("collide", forceCollide<SimNode>().radius((node) => node.radius + (node.kind === "core" ? 9 : 7 - density * 2)))
    .stop()
    .tick(170 + Math.min(150, nodeCount * 2));

  for (const node of nodes) {
    node.x = clamp(node.x ?? WIDTH / 2, node.radius + 18, WIDTH - node.radius - 18);
    node.y = clamp(node.y ?? HEIGHT / 2, node.radius + 18, HEIGHT - node.radius - 24);
  }
  return { nodes, links };
}

function nodeRadius(node: MemoryNetworkNode): number {
  if (node.kind === "core") return 14;
  const importance = clamp(node.importance || 5, 1, 10);
  return 4.2 + importance * 0.45;
}

const TYPE_FILL: Record<string, string> = {
  episodic: "var(--type-episodic)",
  semantic: "var(--type-semantic)",
  procedural: "var(--type-procedural)",
  emotional: "var(--type-emotional)",
  reflective: "var(--type-reflective)"
};

const CORE_FILL = "var(--network-core)";

function nodeFill(node: MemoryNetworkNode): string {
  if (node.kind === "core") return CORE_FILL;
  return TYPE_FILL[node.type || ""] || "var(--muted)";
}

function nodeStroke(node: MemoryNetworkNode): string {
  if (node.kind === "core") return "var(--network-core-stroke)";
  const arousal = node.arousal ?? 0.3;
  return arousal > 0.65 ? "var(--network-hot-stroke)" : "var(--network-node-stroke)";
}

function clientPointToSvg(svg: SVGSVGElement, clientX: number, clientY: number): { x: number; y: number } {
  const rect = svg.getBoundingClientRect();
  return {
    x: ((clientX - rect.left) / Math.max(1, rect.width)) * WIDTH,
    y: ((clientY - rect.top) / Math.max(1, rect.height)) * HEIGHT
  };
}

function constrainView(view: ViewTransform): ViewTransform {
  const scale = clamp(view.scale, MIN_ZOOM, MAX_ZOOM);
  const slackX = WIDTH * scale * 0.7;
  const slackY = HEIGHT * scale * 0.7;
  return {
    scale,
    x: clamp(view.x, -slackX, slackX),
    y: clamp(view.y, -slackY, slackY)
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
