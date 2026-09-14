"""Basic defense: two rockets, one railgun, forward walls and mining economy.

Generic tasks use the judge LLM/sandbox channel; movement and economy stay local.
"""
from collections import Counter
from itertools import permutations, combinations

from .grid import Routes, neighbours
from .memory import Memory
from .geography import Geography
from .economy import Economy
from .tasks import Tasks
from .protocol import Pos, Turn, distance, station_footprint, move_command
from .economy import DAY1_ORE_PHASE_ROUNDS
from .fire_control import plan_fire, select_targets, shot_damage, threat, on_segment

LOADOUT = ("rocket", "rocket", "rocket")   # 要求: 75 金币开局买三座火箭炮
RETURN_MARGIN = 2          # 回到武器旁的少量安全余量
INNER_STAND_SLACK = 8      # 夜间交互站位: 为走到"靠基地内侧"最多多走的步数
RETURN_DEADLINE = 75       # 当天第75回合(含入夜前5回合)前必须回到武器塔旁; 夜间允许移动


def ring(turn, radius):
    station = turn.station()
    if station is None:
        return ()
    footprint = station_footprint(station.pos)
    return tuple(Pos(x, y)
                 for x in range(station.pos.x-radius, station.pos.x+2+radius)
                 for y in range(station.pos.y-1-radius, station.pos.y+1+radius)
                 if min(distance(Pos(x, y), p) for p in footprint) == radius
                 and turn.land(Pos(x, y)))


def wall_sites(turn):
    station = turn.station()
    if station is None:
        return ()
    center_x = station.pos.x + 0.5
    sign = 1 if center_x < turn.width / 2 else -1
    # Forward half of the outer ring. The rear stays open for economic trips.
    return tuple(sorted((p for p in ring(turn, 2) if sign*(p.x-center_x) > 0),
                        key=lambda p: (-sign*(p.x-center_x), abs(p.y-(station.pos.y-0.5)), p.y)))


def decide_response(payload, memory=None):
    memory = memory if memory is not None else Memory()
    turn = Turn.load(payload)
    memory.observe(turn,payload)
    if turn.station() is None or turn.station().health <= 0:
        response = {'roleCommandMap':{},'prompt':'','executeCmd':''}
    else:
        planner = Planner(turn,payload,memory)
        response = {'roleCommandMap':planner.run(), 'prompt':planner.prompt,
                    'executeCmd':planner.execute_cmd}
    memory.remember(turn,response)
    return response


def decide(payload, memory=None):
    # Compatibility for existing standalone users; HTTP uses the full response.
    return decide_response(payload,memory)['roleCommandMap']


class Planner:
    def __init__(self, turn, payload, memory=None):
        self.turn = turn
        self.memory = memory if memory is not None else Memory(day=(turn.round_no-1)//130+1)
        self.geo = Geography(turn)
        self.engaged = set()
        self.prompt = ''
        self.execute_cmd = ''
        self.payload = payload
        self.commands = {}
        self.reserved = set()
        self.gold = turn.gold
        self.built = []
        self.build_targets = set()
        self.planned_towers = set()
        self.prices = {x['name']: float(x['price']) for x in payload.get('vendorShopList', [])}
        self.shop_prices = {x['name']: int(x['price']) for x in payload.get('weaponShopList', [])}
        self.walls = wall_sites(turn)
        self.home_cost_cache = {}
        self.route_cache = {}
        # 可用回合预算到当天第 RETURN_DEADLINE 回合为止（含入夜 5 回合, 夜间可移动）
        self.remaining = max(0, RETURN_DEADLINE - (turn.round_no - 1) % 130)
        self.economic = Economy(self)

    def route(self, role):
        forbidden = set(self.reserved)
        failed = self.memory.failed_steps.get(role.unit_id)
        if failed and failed[1] >= self.turn.round_no:
            forbidden.add(failed[0])
        # A Planner owns one immutable turn. Reservations and failed steps are
        # the only changing route inputs; retain only the latest map per role.
        key = (role.pos, frozenset(forbidden))
        cached = self.route_cache.get(role.unit_id)
        if cached is None or cached[0] != key:
            cached = (key, Routes(self.turn, role, forbidden))
            self.route_cache[role.unit_id] = cached
        return cached[1]

    def move(self, role, routes, stand):
        if stand is None:
            return False
        if self.memory.blocked_goals.get((role.unit_id, stand), 0) > self.turn.round_no:
            return False
        if stand == role.pos:
            return True
        step = routes.first.get(stand)
        if (step is None or step in self.reserved or step in self.build_targets
                or step in self.turn.blocked(role) or not self.turn.land(step)):
            return False
        self.commands[str(role.unit_id)] = move_command(step)
        self.reserved.add(step)
        self.memory.movement[role.unit_id] = stand
        return True

    def inner_stand(self, routes, target, action):
        """夜间交互站位: 只返回严格"靠基地一侧"的相邻格(代价允许时), 否则 None。

        站在围墙外侧会暴露在机器人攻击范围内, 因此夜间对建筑使用券/修复包时,
        要绕到内侧再动手; 内侧不可达(超出允许步数)时返回 None, 由调用方退回
        最短路站位, 保证不会来回打转。
        """
        if self.turn.is_day or action not in ('use', 'build'):
            return None
        station = self.turn.station()
        if station is None:
            return None
        target_d = distance(target, station.pos)
        cands = [q for q in neighbours(target)
                 if q in routes.cost and distance(q, station.pos) < target_d]
        if not cands:
            return None
        nearest = routes.distance(target)
        near = [q for q in cands if routes.cost[q] <= nearest + INNER_STAND_SLACK]
        if not near:
            return None
        return min(near, key=lambda q: (distance(q, station.pos), routes.cost[q], q.x, q.y))

    def interact(self, role, routes, target, action, **fields):
        """与目标交互: 白天走最短路; 夜间优先站到靠基地的内侧再动作。"""
        stand = None
        station = self.turn.station()
        inner = self.inner_stand(routes, target, action)
        if inner is not None and station is not None:
            if routes.distance(target) == 0 and \
                    distance(role.pos, station.pos) <= distance(inner, station.pos):
                stand = role.pos          # 已在可交互位置且不比内侧更外 -> 就地动作
            else:
                stand = inner
        if stand is None:
            stand = routes.adjacent(target)
        previous = self.memory.movement.get(role.unit_id)
        if (stand is not None and stand != role.pos and previous in routes.cost and distance(previous, target) == 1
                and routes.cost[previous] <= routes.cost[stand] + 2
                and self.memory.blocked_goals.get((role.unit_id, previous), 0) <= self.turn.round_no
                and (inner is None or distance(previous, station.pos) <= distance(stand, station.pos))):
            stand = previous
        if (stand is not None and
                self.memory.blocked_goals.get((role.unit_id, stand), 0) > self.turn.round_no):
            candidates = [q for q in neighbours(target) if q in routes.cost
                          and self.memory.blocked_goals.get((role.unit_id, q), 0) <= self.turn.round_no]
            stand = min(candidates, key=lambda q: (routes.cost[q], q.x, q.y), default=None)
        if stand is None:
            return False
        if stand == role.pos:
            self.commands[str(role.unit_id)] = {'action': action, **fields}
        else:
            return self.move(role, routes, stand)
        return True

    def home_cost(self, role, pos):
        if role.unit_id not in self.home_cost_cache:
            from collections import deque
            blocked = self.turn.blocked(role)
            targets = [t.pos for t in self.turn.weapons()] or [self.turn.station().pos]
            seeds = {q for target in targets for q in neighbours(target)
                     if self.turn.land(q) and q not in blocked}
            costs = {q: 0 for q in seeds}
            queue = deque(seeds)
            while queue:
                current = queue.popleft()
                for q in neighbours(current):
                    if q not in costs and q not in blocked and self.turn.land(q):
                        costs[q] = costs[current] + 1
                        queue.append(q)
            self.home_cost_cache[role.unit_id] = costs
        costs = self.home_cost_cache[role.unit_id]
        if pos in costs:
            return costs[pos]
        return min((costs.get(q, 10**6) for q in neighbours(pos)), default=10**6)

    def enough_time(self, role, routes, target, actions=1):
        stand = routes.adjacent(target)
        return stand is not None and (routes.cost[stand] + actions +
                self.home_cost(role, stand) + RETURN_MARGIN < self.remaining)

    def run(self):
        if not self.turn.is_day:
            tasks = Tasks(self)
            tasks.sync_task()
            tasks.receive()
            self.night()
            return self.commands
        if self.payload.get('phaseTask'):
            self.engaged.update(r.unit_id for r in self.turn.alive(('pioneer',)))
        self.economic.prepare()
        # Existing inventory belongs to every role, including the pioneer.
        # Active tasks retain their role; consumables never overwrite task commands.
        for role in self.turn.controllable():
            if role.unit_id in self.engaged or str(role.unit_id) in self.commands:
                continue
            if self.economic.use_consumable(role):
                self.engaged.add(role.unit_id)
            elif role.kind == 'pioneer' and self.economic.upgrade(role, self.route(role)):
                self.engaged.add(role.unit_id)
        # Only active tasks hold the pioneer unconditionally. Existing inventory
        # gets a chance before accepting a new task or starting a treasure trip.
        if self.payload.get('phaseTask'):
            self.engaged.difference_update(r.unit_id for r in self.turn.alive(('pioneer',)))
        Tasks(self).run()
        workers = self.turn.workers()
        maintenance = next((r.unit_id for r in workers
                            if self.memory.jobs.get(r.unit_id,{}).get('type')!='upgrade'),None)
        for role in workers:
            if role.unit_id in self.engaged:
                continue
            routes = self.route(role)
            # Using a voucher in place takes one turn and must not be suppressed
            # by the generic five-turn return margin.
            if self.economic.act_urgent(role,routes):
                continue
            # 回家前: 先判断是否该去商店把闲钱花掉（时机提前到"还够走一趟商店"）
            if self.economic.spend_ready(role, routes) and self.economic.spend_before_home(role, routes):
                continue
            if self.remaining <= self.home_cost(role, role.pos) + RETURN_MARGIN:
                # 再顺手采家门口的矿, 最后回位
                self.economic.harvest_near_home(role, routes)
                continue
            if self.build_tower(role, routes):
                continue
            # 半圈围墙未完成 -> 两名工人一起把墙搭好; 搭好后只留维护工补墙
            if (self.missing_walls() or role.unit_id==maintenance) and self.build_wall(role,routes):
                continue
            self.economic.act(role,routes)
        self.assign_towers(day=True)
        return self.commands

    def build_tower(self, role, routes):
        towers = self.turn.weapons()
        if len(towers) + len(self.built) >= 3 or self.gold < 25:
            return False
        counts = Counter(t.kind for t in towers) + Counter(self.built)
        kind = next((k for k in LOADOUT if counts[k] < LOADOUT.count(k)), None)
        if kind is None:
            return False
        blocked = self.turn.occupied_cells() | self.reserved | self.build_targets
        candidates = [p for p in ring(self.turn, 1) if p not in blocked]
        previous = self.memory.construction_jobs.get(role.unit_id)
        candidates.sort(key=lambda p: (previous != (kind, p), routes.distance(p), p.x, p.y))
        for target in candidates:
            if not self.construction_accessible(target, tower=True):
                continue
            if not self.enough_time(role, routes, target):
                continue
            # Keep at least three distinct free operating cells for the final fleet.
            fleet = [t.pos for t in towers] + list(self.build_targets) + [target]
            seats = set(q for p in fleet for q in neighbours(p)
                        if self.turn.land(q) and q not in blocked and q not in fleet)
            if len(seats) < 3:
                continue
            if self.interact(role, routes, target, 'build', name=kind, targetPos=[target.dump()]):
                self.build_targets.add(target)
                self.planned_towers.add(target)
                self.memory.construction_jobs[role.unit_id] = (kind, target)
                if routes.distance(target) == 0:
                    self.gold -= 25
                    self.built.append(kind)
                    self.reserved.add(target)
                return True
        return False

    def construction_accessible(self, target, tower=False):
        """Keep workers and distinct operating seats connected to the outside."""
        from collections import deque
        free = self.geo.free - self.build_targets
        outside = set(ring(self.turn, 3)) & free
        def reachable(cells):
            seen = outside & cells
            queue = deque(seen)
            while queue:
                for q in neighbours(queue.popleft()):
                    if q in cells and q not in seen:
                        seen.add(q)
                        queue.append(q)
            return seen
        before = reachable(free)
        after = reachable(free - {target})
        if any(r.pos in before and r.pos not in after for r in self.turn.controllable()):
            return False
        fleet = [t.pos for t in self.turn.weapons()] + list(self.planned_towers)
        if tower:
            fleet.append(target)
        seats = [set(neighbours(p)) & after for p in fleet]
        def assign(index, used):
            return index == len(seats) or any(assign(index+1, used | {q})
                                             for q in seats[index] - used)
        return assign(0, set())

    def missing_walls(self):
        occupied = self.turn.occupied_cells() | self.reserved | self.build_targets
        return [p for p in self.walls if p not in occupied]

    def build_wall(self, role, routes):
        missing = self.missing_walls()
        if not missing:
            return False
        stock = role.backpack.count('stone')
        batch = min(3, (len(missing)+1)//2)
        # 要求3: 第1天前 30 回合先全员采铁/铜赚钱, 之后再采石修墙(不提前耗在采石上)
        if self.memory.day == 1 and (self.turn.round_no - 1) % 130 + 1 <= DAY1_ORE_PHASE_ROUNDS:
            return False
        # 采石分工: 只有被指派采石的工人去攒石头，另一名工人留给经济模块采矿石，
        # 避免"两人都去采石头"导致矿石收入为零（需求2）。
        # 半圈围墙没搭完之前, 两名工人都可以采石建墙（先把防线立起来）;
        # 半圈搭好后不需要石头, 两人一起采矿（分工自然退化为全员采矿）。
        stone_fetcher = (self.economic.family(role) == 'stone'
                         or bool(missing))
        nearby = [p for p, kind in self.turn.zones.items()
                  if kind == 'stone' and distance(role.pos, p) <= 1
                  and not self.economic.blocked(p,kind)
                  and self.economic.mine_claims.get(p, 0) == 0]
        if stone_fetcher and nearby and stock < batch and not role.backpack_full:
            target = nearby[0]
            if self.enough_time(role, routes, target, batch-stock):
                return self.interact(role, routes, target, 'collect', targetPos=[target.dump()])
        if not stock and stone_fetcher:
            # Once established, reserve only a small stone batch per trip.
            mines = [p for p, kind in self.turn.zones.items()
                     if kind == 'stone' and not self.economic.blocked(p,kind)
                     and self.economic.mine_claims.get(p, 0) == 0]
            for target in sorted(mines, key=routes.distance):
                if not role.backpack_full and self.enough_time(role, routes, target, 3):
                    self.economic.mine_claims[target] += 1      # 认领, 避免两人同矿
                    return self.interact(role, routes, target, 'collect', targetPos=[target.dump()])
            return False
        if not stock:
            return False        # 非采石工且手上无石头 -> 交给经济模块去采矿石
        previous = self.memory.construction_jobs.get(role.unit_id)
        for target in sorted(missing, key=lambda p: (previous != ('wall', p),
                                                    self.walls.index(p)//4, routes.distance(p))):
            if not self.construction_accessible(target):
                continue
            if self.enough_time(role, routes, target) and self.interact(
                    role, routes, target, 'build', name='wall', targetPos=[target.dump()]):
                self.build_targets.add(target)
                self.memory.construction_jobs[role.unit_id] = ('wall', target)
                if routes.distance(target) == 0:
                    self.reserved.add(target)
                return True
        return False

    def upgrade_options(self):
        return self.economic.options()

    def deliver_upgrade(self,role,routes):
        self.economic.prepare()
        return self.economic.upgrade(role,routes)

    def economy(self,role,routes):
        self.economic.prepare()
        return self.economic.act(role,routes)

    def assign_towers(self, day=False):
        roles = [r for r in self.turn.controllable() if str(r.unit_id) not in self.commands and r.unit_id not in self.engaged]
        towers = list(self.turn.weapons())
        if not roles or not towers:
            return []
        paths = {r.unit_id: self.route(r) for r in roles}
        best, best_cost = [], None
        for count in range(min(len(roles), len(towers)), 0, -1):
            for chosen in combinations(roles, count):
                for fleet in permutations(towers, count):
                    distances = [paths[r.unit_id].distance(t.pos) for r,t in zip(chosen,fleet)]
                    if any(d >= 10**6 for d in distances):
                        continue
                    switching = sum(3 for r,t in zip(chosen,fleet)
                                    if self.memory.tower_assignments.get(r.unit_id,t.unit_id) != t.unit_id)
                    cost = (-sum(d == 0 for d in distances), sum(distances)+switching,
                            sum(t.kind != 'rocket' for t in fleet))
                    if best_cost is None or cost < best_cost:
                        best_cost, best = cost, list(zip(chosen, fleet))
            if best:
                break
        ready = []
        for role, tower in best:
            routes = self.route(role)
            self.memory.tower_assignments[role.unit_id] = tower.unit_id
            if distance(role.pos, tower.pos) <= 1:
                ready.append((role, tower))
            else:
                seats = [q for q in neighbours(tower.pos) if q in routes.cost
                         and self.memory.blocked_goals.get((role.unit_id, q), 0) <= self.turn.round_no]
                self.move(role, routes, min(seats, key=lambda q: (routes.cost[q], q.x, q.y), default=None))
        return ready

    def night(self):
        # Defense owns all available operators at night. No delivery trip may
        # remove a controller; cooldown/idle turns may use items in place only.
        self.economic.prepare()
        ready = self.assign_towers()
        plan, _ = plan_fire(self.turn, [tower for _,tower in ready])
        serviced = set()
        for role, tower in ready:
            targets = plan.get(tower.unit_id)
            if targets:
                self.commands[str(tower.unit_id)] = {'action': 'attack', 'controllerId': str(role.unit_id),
                                                     'targetPos': [p.dump() for p in targets]}
            elif not self.economic.use_consumable(role):
                options = self.economic.held_options(role)
                adjacent = [o for o in options if distance(role.pos, o[4].pos) <= 1
                            and o[2] not in serviced and o[2] not in plan]
                if adjacent:
                    _,_,_,item,target = min(adjacent, key=lambda o:(o[0],o[2]))
                    self.commands[str(role.unit_id)] = {'action':'use','name':item,
                                                        'targetPos':[target.pos.dump()]}
                    serviced.add(target.unit_id)
