from dataclasses import dataclass
from itertools import combinations

from app.memory.models import MemoryRecord
from app.memory.network import _memory_similarity
from app.memory.store import MemoryStore


@dataclass(frozen=True)
class TraversalEdge:
    source: str
    target: str
    weight: float
    kind: str = "similarity"
    label: str = "相似"


@dataclass(frozen=True)
class TraversalResult:
    memory: MemoryRecord
    score: float
    depth: int
    path: list[TraversalEdge]


@dataclass(frozen=True)
class TraversalMeta:
    depth: int
    limit: int
    similarity_threshold: float
    candidate_count: int
    edge_count: int
    reachable_count: int
    iterations: int
    converged: bool


@dataclass(frozen=True)
class TraversalResponse:
    seed: MemoryRecord
    results: list[TraversalResult]
    meta: TraversalMeta


def traverse_memory_network(
    *,
    store: MemoryStore,
    user_id: str,
    seed_id: str,
    depth: int = 2,
    limit: int = 10,
    similarity_threshold: float = 0.42,
    max_candidates: int = 500,
    max_edges: int = 1500,
    damping_factor: float = 0.85,
    max_iterations: int = 60,
    convergence_threshold: float = 1e-6,
) -> TraversalResponse | None:
    seed = store.get_memory(memory_id=seed_id, user_id=user_id)
    if seed is None or seed.status == "archived":
        return None

    capped_depth = max(1, min(depth, 3))
    capped_limit = max(1, min(limit, 50))
    capped_candidates = max(2, min(max_candidates, 1000))
    capped_edges = max(0, min(max_edges, 5000))
    threshold = max(0.0, min(similarity_threshold, 1.0))

    memories = store.list_memories(user_id=user_id, limit=capped_candidates)
    memory_by_id = {memory.id: memory for memory in memories}
    if seed.id not in memory_by_id:
        memories = [seed, *memories]
        memory_by_id[seed.id] = seed

    edges = _build_similarity_edges(
        memories=memories,
        threshold=threshold,
        max_edges=capped_edges,
    )
    adjacency = _build_adjacency(edges)
    reachable_paths = _reachable_paths(
        adjacency=adjacency,
        seed_id=seed.id,
        max_depth=capped_depth,
    )
    reachable_ids = set(reachable_paths)
    reachable_ids.add(seed.id)
    ranks, iterations, converged = _personalized_pagerank(
        adjacency=adjacency,
        node_ids=reachable_ids,
        seed_id=seed.id,
        damping_factor=damping_factor,
        max_iterations=max_iterations,
        convergence_threshold=convergence_threshold,
    )

    ranked_ids = sorted(
        (memory_id for memory_id in reachable_paths if memory_id in memory_by_id),
        key=lambda memory_id: (
            ranks.get(memory_id, 0.0),
            _path_weight(reachable_paths[memory_id]),
            -len(reachable_paths[memory_id]),
        ),
        reverse=True,
    )
    results = [
        TraversalResult(
            memory=memory_by_id[memory_id],
            score=ranks.get(memory_id, 0.0),
            depth=len(reachable_paths[memory_id]),
            path=reachable_paths[memory_id],
        )
        for memory_id in ranked_ids[:capped_limit]
    ]

    return TraversalResponse(
        seed=seed,
        results=results,
        meta=TraversalMeta(
            depth=capped_depth,
            limit=capped_limit,
            similarity_threshold=threshold,
            candidate_count=len(memories),
            edge_count=len(edges),
            reachable_count=max(0, len(reachable_ids) - 1),
            iterations=iterations,
            converged=converged,
        ),
    )


def _build_similarity_edges(
    *,
    memories: list[MemoryRecord],
    threshold: float,
    max_edges: int,
) -> list[TraversalEdge]:
    scored_edges: list[TraversalEdge] = []
    for left, right in combinations(memories, 2):
        score = _memory_similarity(left, right)
        if score < threshold:
            continue
        scored_edges.append(
            TraversalEdge(
                source=left.id,
                target=right.id,
                weight=round(score, 6),
            )
        )
    scored_edges.sort(key=lambda edge: edge.weight, reverse=True)
    return scored_edges[:max_edges] if max_edges else []


def _build_adjacency(edges: list[TraversalEdge]) -> dict[str, list[tuple[str, TraversalEdge]]]:
    adjacency: dict[str, list[tuple[str, TraversalEdge]]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source, []).append((edge.target, edge))
        adjacency.setdefault(edge.target, []).append((edge.source, edge))
    return adjacency


def _reachable_paths(
    *,
    adjacency: dict[str, list[tuple[str, TraversalEdge]]],
    seed_id: str,
    max_depth: int,
) -> dict[str, list[TraversalEdge]]:
    paths: dict[str, list[TraversalEdge]] = {}
    frontier = [seed_id]
    for _ in range(max_depth):
        next_frontier: list[str] = []
        for current_id in frontier:
            current_path = paths.get(current_id, [])
            for neighbor_id, edge in adjacency.get(current_id, []):
                if neighbor_id == seed_id:
                    continue
                candidate_path = [*current_path, edge]
                existing_path = paths.get(neighbor_id)
                if existing_path is not None and (
                    len(existing_path) < len(candidate_path)
                    or (
                        len(existing_path) == len(candidate_path)
                        and _path_weight(existing_path) >= _path_weight(candidate_path)
                    )
                ):
                    continue
                paths[neighbor_id] = candidate_path
                next_frontier.append(neighbor_id)
        frontier = next_frontier
        if not frontier:
            break
    return paths


def _personalized_pagerank(
    *,
    adjacency: dict[str, list[tuple[str, TraversalEdge]]],
    node_ids: set[str],
    seed_id: str,
    damping_factor: float,
    max_iterations: int,
    convergence_threshold: float,
) -> tuple[dict[str, float], int, bool]:
    if not node_ids:
        return {}, 0, True

    damping = max(0.0, min(damping_factor, 0.99))
    ranks = {node_id: 1.0 / len(node_ids) for node_id in node_ids}
    personalization = {node_id: 0.0 for node_id in node_ids}
    personalization[seed_id] = 1.0

    for iteration in range(1, max_iterations + 1):
        next_ranks = {
            node_id: (1.0 - damping) * personalization[node_id]
            for node_id in node_ids
        }
        dangling_score = 0.0
        for node_id in node_ids:
            neighbors = [
                (neighbor_id, edge)
                for neighbor_id, edge in adjacency.get(node_id, [])
                if neighbor_id in node_ids
            ]
            total_weight = sum(edge.weight for _, edge in neighbors)
            if not neighbors or total_weight <= 0:
                dangling_score += ranks[node_id]
                continue
            for neighbor_id, edge in neighbors:
                next_ranks[neighbor_id] += damping * ranks[node_id] * (edge.weight / total_weight)
        if dangling_score:
            for node_id in node_ids:
                next_ranks[node_id] += damping * dangling_score * personalization[node_id]

        delta = sum(abs(next_ranks[node_id] - ranks[node_id]) for node_id in node_ids)
        ranks = next_ranks
        if delta < convergence_threshold:
            return ranks, iteration, True
    return ranks, max_iterations, False


def _path_weight(path: list[TraversalEdge]) -> float:
    score = 1.0
    for edge in path:
        score *= edge.weight
    return score
