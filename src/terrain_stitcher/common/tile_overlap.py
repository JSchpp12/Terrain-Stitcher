from __future__ import annotations

from collections import defaultdict

from .geometry import buildRTree, toProjected, toPolygon


def FindOverlappingChunks(bounds, threshold=0.3):
    projectPolygons = [toProjected(toPolygon(b)) for b in bounds]
    rTreeIdx = buildRTree(projectPolygons)

    overlaps = set()

    for i, poly in enumerate(projectPolygons):
        for j in rTreeIdx.intersection(poly.bounds):
            if i >= j:
                continue  # avoid duplicate or self comparison

            poly_j = projectPolygons[j]
            if not poly.intersects(poly_j):
                continue

            intersection_area = poly.intersection(poly_j).area
            min_area = min(poly.area, poly_j.area)
            if min_area == 0:
                continue

            overlap_ratio = intersection_area / min_area
            if overlap_ratio >= threshold:
                overlaps.add((i, j, overlap_ratio))

    return sorted(overlaps, key=lambda x: -x[2])


def GroupOverlappingChunks(overlapPairs, numChunks) -> list:
    # Only group chunks that directly overlap
    # Isolated chunks (no overlap) are kept as their own singleton
    merged = set()
    groups = []

    # Build adjacency from direct overlap pairs only
    graph = defaultdict(set)
    for i, j, _ in overlapPairs:
        graph[i].add(j)
        graph[j].add(i)

    visited = set()

    def dfs(node, group):
        visited.add(node)
        group.add(node)
        for neighbor in graph[node]:
            if neighbor not in visited:
                dfs(neighbor, group)

    # Only run DFS on nodes that actually have overlaps
    for node in graph:
        if node not in visited:
            group = set()
            dfs(node, group)
            groups.append(group)
            merged.update(group)

    # Add all non-overlapping chunks as singletons
    for i in range(numChunks):
        if i not in merged:
            groups.append({i})

    return groups


def SelectRepresentatives(groups, boundsList, criteria="min_index"):
    selected = []

    for group in groups:
        if criteria == "min_index":
            chosen = min(group)
        elif criteria == "max_area":
            chosen = max(
                group, key=lambda idx: toProjected(toPolygon(boundsList[idx])).area
            )
        else:
            raise ValueError("Unknown criteria")
        selected.append(chosen)

    return selected

