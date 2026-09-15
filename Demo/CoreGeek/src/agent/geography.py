"""Exact obstacle-aware distances for multi-leg economic journeys."""
from collections import deque
from .grid import neighbours
from .protocol import Pos, distance, station_footprint

INF = 10**6


class Geography:
    def __init__(self, turn):
        self.turn = turn
        blocked = set(turn.zones)
        for unit in turn.ours + turn.enemies:
            if unit.health > 0 and unit.kind not in ('worker','pioneer'):
                blocked.update(turn.footprint(unit))
        blocked.update(r.pos for r in turn.robots if r.health > 0)
        self.free = {Pos(x,y) for x in range(turn.width) for y in range(turn.height)
                     if Pos(x,y) not in blocked}
        self.edges = {p: tuple(q for q in neighbours(p) if q in self.free) for p in self.free}
        self.cache = {}

    def seats(self, target):
        return tuple(q for q in neighbours(target) if q in self.free)

    def field(self, seeds):
        key = tuple(sorted(set(seeds), key=lambda q:(q.x,q.y)))
        if key not in self.cache:
            cost = {q:0 for q in key if q in self.free}
            queue = deque(cost)
            while queue:
                current = queue.popleft()
                for q in self.edges[current]:
                    if q not in cost:
                        cost[q] = cost[current]+1
                        queue.append(q)
            self.cache[key] = cost
        return self.cache[key]

    def to(self, target):
        return self.field(self.seats(target))


# 夜战结束判定用的威胁半径(与火箭炮 1 级射程 10 对齐): 基地/炮位该半径内没有活机器人
# 才算"阵前清空"。放在这里是为了让 brain(决策) 与 memory(跨回合去抖) 共用同一判定。
THREAT_RADIUS = 10


def battle_over(turn, radius=THREAT_RADIUS):
    """阵前是否已经没有活着的机器人(以武器塔为锚点, 无塔时以基地占地为锚点)。"""
    anchors = [t.pos for t in turn.weapons()]
    if not anchors:
        station = turn.station()
        anchors = list(station_footprint(station.pos)) if station is not None else []
    if not anchors:
        return True
    return not any(r.health > 0 and min(distance(r.pos, p) for p in anchors) <= radius
                   for r in turn.robots)
