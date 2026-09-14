"""Eight-direction shortest paths, including reservations for this turn."""
from collections import deque
from .protocol import Pos, Turn, Unit


def neighbours(pos: Pos):
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                yield Pos(pos.x + dx, pos.y + dy)


class Routes:
    def __init__(self, turn: Turn, moving: Unit, reserved=()):
        blocked = turn.blocked(moving) | frozenset(reserved)
        self.start = moving.pos
        self.cost = {moving.pos: 0}
        self.first = {}
        self._adjacent = {}
        queue = deque([moving.pos])
        while queue:
            current = queue.popleft()
            for pos in neighbours(current):
                if pos in self.cost or pos in blocked or not turn.land(pos):
                    continue
                self.cost[pos] = self.cost[current] + 1
                self.first[pos] = pos if current == moving.pos else self.first[current]
                queue.append(pos)

    def adjacent(self, target: Pos):
        if target not in self._adjacent:
            self._adjacent[target] = min(
                (p for p in neighbours(target) if p in self.cost),
                key=lambda p: (self.cost[p], p.x, p.y), default=None)
        return self._adjacent[target]

    def distance(self, target: Pos):
        stand = self.adjacent(target)
        return self.cost[stand] if stand is not None else 10**6


def next_step(turn: Turn, moving: Unit, goal: Pos) -> Pos | None:
    return Routes(turn, moving).first.get(goal)
