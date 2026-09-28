"""Rank-only fusion; missing channels contribute zero, ties use stable IDs."""


def reciprocal_rank_fusion(rankings: list[list[dict]], k: int = 60) -> list[dict]:
    if k < 1:
        raise ValueError('RRF k must be positive')
    fused = {}
    for channel, ranking in enumerate(rankings):
        seen = set()
        rank = 0
        for hit in ranking:
            identifier = hit['id']
            if identifier in seen:
                continue
            seen.add(identifier)
            rank += 1
            row = fused.setdefault(identifier, {'id': identifier, 'score': 0.0, 'ranks': {}})
            row['score'] += 1.0 / (k + rank)
            row['ranks'][str(channel)] = rank
    return sorted(fused.values(), key=lambda row: (-row['score'], row['id']))
