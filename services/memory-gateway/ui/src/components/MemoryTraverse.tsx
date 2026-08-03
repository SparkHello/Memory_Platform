import { ChevronDown, ChevronUp, GitBranch } from "lucide-react";
import { useState } from "react";
import type { TraversalResponse } from "../types";
import { displayText, percent, shortId } from "../utils/format";
import { FieldList } from "./FormControls";

export function MemoryTraverse({
  traverse,
  loading,
  error
}: {
  traverse: TraversalResponse | null;
  loading: boolean;
  error: string | null;
}) {
  const [showMeta, setShowMeta] = useState(false);

  if (loading) {
    return (
      <section className="subpanel traverse-panel">
        <h3>图遍历（实验）</h3>
        <div className="state-block">
          <GitBranch size={18} className="spin" />
          正在遍历记忆网络...
        </div>
      </section>
    );
  }

  if (error) {
    return (
      <section className="subpanel traverse-panel">
        <h3>图遍历（实验）</h3>
        <div className="notice warning">
          <GitBranch size={16} />
          {error}
        </div>
      </section>
    );
  }

  if (!traverse) {
    return null;
  }

  if (!traverse.results.length) {
    return (
      <section className="subpanel traverse-panel">
        <h3>图遍历（实验）</h3>
        <div className="state-block empty compact">
          未找到可达的相关记忆（depth={traverse.meta.depth}，
          candidates={traverse.meta.candidate_count}）
        </div>
      </section>
    );
  }

  return (
    <section className="subpanel traverse-panel">
      <h3>
        图遍历（实验） · {traverse.results.length} 条结果
        <span className="traverse-breadcrumb">
          depth={traverse.meta.depth} · threshold=
          {Math.round(traverse.meta.similarity_threshold * 100)}%
        </span>
      </h3>

      {/* Seed summary */}
      <div className="traverse-seed">
        <span className="detail-eyebrow">种子记忆</span>
        <p>{traverse.seed.content}</p>
        <div className="traverse-seed-meta">
          <span>{displayText(traverse.seed.type)}</span>
          <span>重要度 {traverse.seed.importance}</span>
          {traverse.seed.redacted && (
            <span className="traverse-redacted-tag">已遮罩</span>
          )}
        </div>
      </div>

      {/* Results */}
      <ol className="traverse-results">
        {traverse.results.map((item, index) => (
          <li key={item.memory.id} className="traverse-result-item">
            <div className="traverse-result-rank">
              <span className="traverse-rank-num">{index + 1}</span>
              <span className="traverse-rank-score">
                {item.score.toFixed(4)}
              </span>
            </div>
            <div className="traverse-result-body">
              <p className="traverse-result-content">
                {item.memory.redacted ? "[已遮罩]" : item.memory.content}
              </p>
              <div className="traverse-result-meta">
                <span>
                  {item.depth === 1
                    ? "直达"
                    : `${item.depth} 跳`}
                </span>
                <span>{displayText(item.memory.type)}</span>
                <span>置信度 {percent(item.memory.confidence)}</span>
                <span className="traverse-result-id">{shortId(item.memory.id)}</span>
              </div>
              {item.path.length > 0 && (
                <div className="traverse-path">
                  {item.path.map((edge, edgeIndex) => (
                    <span key={edgeIndex} className="traverse-path-edge">
                      {shortId(edge.source)} → {shortId(edge.target)}
                      <span className="traverse-path-weight">
                        ({edge.weight.toFixed(3)})
                      </span>
                    </span>
                  ))}
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>

      {/* Meta toggle */}
      <button
        className="traverse-meta-toggle"
        type="button"
        onClick={() => setShowMeta(!showMeta)}
      >
        {showMeta ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        遍历详情
      </button>
      {showMeta && (
        <div className="traverse-meta">
          <FieldList
            compact
            entries={[
              ["候选记忆数", traverse.meta.candidate_count],
              ["边数量", traverse.meta.edge_count],
              ["可达节点数", traverse.meta.reachable_count],
              ["PageRank 迭代", traverse.meta.iterations],
              ["已收敛", traverse.meta.converged ? "是" : "否"],
              ["深度限制", traverse.meta.depth],
              ["结果上限", traverse.meta.limit],
              [
                "相似度阈值",
                `${Math.round(traverse.meta.similarity_threshold * 100)}%`
              ]
            ]}
          />
        </div>
      )}
    </section>
  );
}
