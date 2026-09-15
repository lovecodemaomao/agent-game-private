"""Basic defense: two rockets, one railgun, forward walls and mining economy.

Generic tasks use the judge LLM/sandbox channel; movement and economy stay local.
"""
from collections import Counter
from dataclasses import replace
from itertools import permutations, combinations
import math

from .grid import Routes, neighbours
from .memory import Memory
from .geography import Geography, INF
from .economy import Economy
from .tasks import Tasks
from .protocol import (Pos, Turn, build_command, distance, move_command,
                       station_footprint)
from .economy import (DAY1_ORE_PHASE_ROUNDS, DAY_ROUNDS, WALL_MAINTENANCE_DAY)
from .fire_control import plan_fire, select_targets, shot_damage, threat, on_segment
from . import fire_control as combat

LOADOUT = ("rocket", "rocket", "railgun")
RETURN_MARGIN = 2          # 回到武器旁的少量安全余量
INNER_STAND_SLACK = 8      # 夜间交互站位: 为走到"靠基地内侧"最多多走的步数
RETURN_DEADLINE = 75       # Legacy economic horizon; gated bases enforce day 70 below.
PREPOSITION_SAFE_RADIUS = 8   # 夜间预置站位: 目的地该半径内还有机器人就不去
THREAT_RADIUS = 10            # 夜战结束判定: 基地/炮位该半径内还有活机器人就继续防守
                              #   (与火箭炮 1 级射程 10 对齐; 不再用固定夜末回合数等待)


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


def wall_sites(turn, extend=0):
    """迎敌半圈围墙格(由前到后排序)。

    extend>0 时沿围墙弧线在两端各再补 extend 格(需求5: 第3天把半圈从 10 格加长到 12 格)。
    """
    station = turn.station()
    if station is None:
        return ()
    center_x = station.pos.x + 0.5
    sign = 1 if center_x < turn.width / 2 else -1
    # Forward half of the outer ring. The rear stays open for economic trips.
    sites = [p for p in ring(turn, 2) if sign*(p.x-center_x) > 0]
    day = (turn.round_no-1)//130+1
    if day >= 3:
        sites = list(ring(turn, 2))
    ordered = tuple(sorted(sites, key=lambda p: (-sign*(p.x-center_x),
                                              abs(p.y-(station.pos.y-0.5)), p.y)))
    return ordered[:4] if day == 1 else ordered


def _arc_extension(turn, station, sites, count):
    """沿环线在围墙弧的两端各补 count 格(按绕基地的角度顺序找相邻的下一格)。"""
    ordered = sorted(ring(turn, 2), key=lambda p: math.atan2(p.y-(station.pos.y-0.5),
                                                              p.x-(station.pos.x+0.5)))
    index = {p: i for i, p in enumerate(ordered)}
    total = len(ordered)
    known = [index[p] for p in sites if p in index]
    if not known or len(known) == total:
        return []
    known_set = set(known)
    start = min(known)
    for _ in range(total):
        if (start-1) % total in known_set:
            start = (start-1) % total
        else:
            break
    end = start
    for _ in range(total):
        if (end+1) % total in known_set:
            end = (end+1) % total
        else:
            break
    extra = [ordered[(start-k) % total] for k in range(1, count+1)]
    extra += [ordered[(end+k) % total] for k in range(1, count+1)]
    return [p for p in extra if turn.land(p)]


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
        # 需求5: 第3天起迎敌半圈围墙两端各加一格(10 -> 12 格)
        self.day = self.memory.day or ((turn.round_no-1)//130+1)
        self.walls = wall_sites(turn)
        self.home_cost_cache = {}
        self.route_cache = {}
        self.select_gate()
        # 可用回合预算: 白天到当天第 RETURN_DEADLINE 回合为止(含入夜 5 回合)。
        # 夜间在"夜战结束"后可以直接开工(需求4: 夜间可采矿/做任务), 此时把预算
        # 设成"一个完整白天"(RETURN_DEADLINE) —— 与次日白天的预算一致, 这样夜间
        # 选定的矿点/行程到次日清晨不会被重新规划成另一条路线(否则来回摇摆)。
        # 夜战未结束则预算为 0: 只守不干活。
        # 夜间是否处于"开工"状态: 用 memory 的状态锁(连续清空才开工), 不是逐回合瞬时判定
        self.battle_over = (self.memory.night_mode == 'work')
        if turn.is_day:
            self.remaining = max(0, (70 if self.day>=3 else RETURN_DEADLINE) - (turn.round_no - 1) % 130)
        elif self.battle_over:
            self.remaining = RETURN_DEADLINE
        else:
            self.remaining = 0
        self.economic = Economy(self)

    def base_distance(self, pos):
        return min(distance(pos,q) for q in station_footprint(self.turn.station().pos))

    def select_gate(self):
        station = self.turn.station()
        if station is None:
            return
        sites = ring(self.turn,2)
        if self.memory.gate_pos in sites:
            return
        sign = 1 if station.pos.x+.5 < self.turn.width/2 else -1
        existing = {w.pos:w for w in self.turn.walls()}
        candidates = [p for p in sites if p not in existing or existing[p].level == 1]
        candidates = [p for p in candidates if any(self.base_distance(q)==1 and self.turn.land(q)
                      and q not in {t.pos for t in self.turn.weapons()} for q in neighbours(p))
                      and any(self.base_distance(q)==3 and self.turn.land(q) for q in neighbours(p))]
        self.memory.gate_pos = min(candidates, key=lambda p:(sign*(p.x-station.pos.x),
            abs(p.y-(station.pos.y-.5)),p.y), default=None)

    def wall_group(self, pos):
        if pos == self.memory.gate_pos:
            return 3
        station = self.turn.station()
        sign = 1 if station.pos.x+.5 < self.turn.width/2 else -1
        projection = sign*(pos.x-(station.pos.x+.5))
        return 0 if projection >= 2 else 2 if projection <= -2 else 1

    def gate_wall(self):
        return next((w for w in self.turn.walls() if w.pos == self.memory.gate_pos),None)

    def manage_gate(self):
        """Observed-state daytime transaction. Never assume remove/build succeeded."""
        gate = self.memory.gate_pos
        if not self.turn.is_day or self.day < 3 or gate is None:
            return False
        wall = self.gate_wall()
        self.memory.gate_state = 'closed' if wall else 'open'
        roles = self.turn.controllable()
        workers = self.turn.workers()
        if not workers:
            return False
        rod = (self.turn.round_no-1)%130+1
        needs_exit = (any(self.base_distance(r.pos)>1 for r in roles)
                      or bool(self.memory.jobs) or bool(self.memory.task)
                      or any(self.base_distance(p)>2 and kind in
                             ('stone','iron','copper','task','taskPoint')
                             for p,kind in self.turn.zones.items())
                      or bool(self.turn.gold and (self.prices or self.shop_prices)))
        if wall and rod < 50 and needs_exit:
            worker = min(workers,key=lambda r:(self.route(r).distance(gate),r.unit_id))
            if wall.level == 1 and self.interact(worker,self.route(worker),gate,'remove',targetPos=[gate.dump()]):
                self.engaged.add(worker.unit_id)
                self.memory.gate_state = 'opening'
            return False
        paths = {r.unit_id:self.route(r) for r in roles}
        outside = [r for r in roles if self.base_distance(r.pos)>1]
        # A role waiting in the gate is a temporary reservation, not an
        # unreachable map. Using INF here previously froze construction and
        # oscillated the gate keeper as early as the first daytime turns.
        structural_turn = replace(self.turn,ours=tuple(u for u in self.turn.ours
                                  if u.kind not in ('worker','pioneer')))
        home = {}
        for r in outside:
            budget_path = Routes(structural_turn,r,self.parking_cells())
            home[r.unit_id] = min((budget_path.cost.get(q,INF) for q in ring(self.turn,1)),default=INF)
        return_now = rod >= min(60,70-max(home.values(),default=0)-len(roles)-2)
        if not return_now:
            return False
        keeper = min(workers,key=lambda r:('stone' not in r.backpack,
            self.route(r).distance(gate),r.unit_id))
        self.memory.gate_keeper = keeper.unit_id
        for r in roles:
            self.engaged.add(r.unit_id)
            self.memory.jobs.pop(r.unit_id,None)
            if self.base_distance(r.pos)>1:
                seats = [q for q in ring(self.turn,1) if q in self.route(r).cost
                         and q not in self.reserved]
                self.move(r,self.route(r),min(seats,key=lambda q:(self.route(r).cost[q],q.x,q.y),default=None))
        if not outside and wall is None and gate not in self.turn.occupied_cells():
            routes = self.route(keeper)
            if 'stone' in keeper.backpack:
                seats = [q for q in neighbours(gate) if self.base_distance(q)==1 and q in routes.cost]
                stand = min(seats,key=lambda q:(routes.cost[q],q.x,q.y),default=None)
                if stand == keeper.pos:
                    self.commands[str(keeper.unit_id)] = build_command(gate,'wall')
                    self.memory.gate_state = 'closing'
                else:
                    self.move(keeper,routes,stand)
            else:
                self.memory.event('gate remains open: no stone before dawn deadline')
        elif not outside and wall is not None:
            self.engaged.clear()
            self.assign_towers(day=True)
        return True

    def clear_construction_seats(self):
        """An idle pioneer must not occupy a corner wall's sole interior build seat."""
        if self.payload.get('phaseTask') or self.memory.task:
            return
        seats = set()
        inner = set(ring(self.turn,1))
        for pos in self.missing_walls():
            possible = set(neighbours(pos)) & inner
            if len(possible)==1:
                seats |= possible
        for role in self.turn.alive(('pioneer',)):
            if (role.pos not in seats and role.pos not in self.walls
                    or str(role.unit_id) in self.commands):
                continue
            routes=Routes(self.turn,role,self.reserved | self.build_targets)
            candidates=[q for q in (inner-seats) | set(ring(self.turn,3))
                        if q in routes.cost and q not in self.reserved]
            stand=min(candidates,key=lambda q:(routes.cost[q],q.x,q.y),default=None)
            if stand is not None and self.move(role,routes,stand):
                self.engaged.add(role.unit_id)
            elif stand is None:
                # An operator can trap the pioneer in a corner's only build
                # seat. Yield that operator first, then clear the pioneer on
                # the next observed turn; never assume simultaneous movement.
                for blocker in self.turn.controllable():
                    if (blocker.unit_id == role.unit_id or distance(blocker.pos,role.pos)!=1
                            or blocker.unit_id in self.engaged):
                        continue
                    path = Routes(self.turn,blocker,self.reserved | self.build_targets)
                    exits = [q for q in (inner-seats) | set(ring(self.turn,3)) if q in path.cost and q != blocker.pos
                             and q not in self.reserved]
                    dest = min(exits,key=lambda q:(path.cost[q],q.x,q.y),default=None)
                    if dest is not None and self.move(blocker,path,dest):
                        self.engaged.add(blocker.unit_id)
                        break

    def route(self, role):
        forbidden = set(self.reserved) | self.parking_cells()
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

    def parking_cells(self):
        """仍待建造的迎敌半圈围墙格 + 计划建造的炮位: 任何角色都不得停留其上。

        人物一旦站上这些格子, 该格就被占住而建不了墙/炮(需求1: 不能卡在
        面对机器人的那一圈里), 所以把它们从寻路图里排除 —— 既不停留也不穿行,
        路径会自动绕开这一圈, 不必事后纠偏。
        """
        standing = {w.pos for w in self.turn.walls()} | {t.pos for t in self.turn.weapons()}
        return ({p for p in self.walls if p not in standing and p != self.memory.gate_pos}
                | {p for p in self.build_targets if p not in standing})

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

    def home_cost(self, role, pos, avoid_parking=False):
        """回到武器塔旁的最少回合数。

        avoid_parking=True 时把"待建围墙格"也算作障碍 —— 实际寻路(route)确实要绕开
        那一圈(需求1), 只用 turn.blocked 会低估返程(实测低估数回合, 导致开拓者在商店
        待太久、天黑了还没到塔)。
        """
        key = (role.unit_id, bool(avoid_parking))
        if key not in self.home_cost_cache:
            from collections import deque
            blocked = self.turn.blocked(role)
            if avoid_parking:
                blocked = blocked | frozenset(self.parking_cells())
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
            self.home_cost_cache[key] = costs
        costs = self.home_cost_cache[key]
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
            if self.battle_over:
                # 需求4: 夜战结束后开拓者立即去任务点继续做任务(领取/执行/提交)
                tasks.run()
            else:
                tasks.sync_task()
                tasks.receive()
            self.night()
            return self.commands
        if self.manage_gate():
            return self.commands
        self.clear_construction_seats()
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
        self.pioneer_standby()
        workers = self.turn.workers()
        maintenance = next((r.unit_id for r in workers
                            if self.memory.jobs.get(r.unit_id,{}).get('type')!='upgrade'),None)
        for role in workers:
            if role.unit_id in self.engaged or str(role.unit_id) in self.commands:
                continue
            routes = self.route(role)
            job = self.memory.jobs.get(role.unit_id,{})
            target = self.economic.target(job) if job.get('type')=='upgrade' else None
            if target and job.get('bought') and routes.distance(target.pos)==0 and self.economic.upgrade(role,routes):
                continue
            if (self.day >= 3 and not self.gate_wall()
                    and not any('stone' in w.backpack for w in workers)
                    and role.unit_id == min(w.unit_id for w in workers)
                    and self.fetch_stone(role,routes,1)):
                continue
            if self.economic.maintain_wall(role,routes):
                continue
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

    def pioneer_standby(self):
        """需求2: 开拓者没有任务可做时去商店旁待命并按计划买券, 不在基地空转。

        任务/寻宝优先级更高(Tasks.run 已先跑过); 只有"确实无事可做"才去商店。
        """
        pioneer = next(iter(self.turn.alive(('pioneer',))), None)
        if pioneer is None or pioneer.unit_id in self.engaged:
            return False
        if str(pioneer.unit_id) in self.commands:
            return False
        if self.memory.task is not None or self.memory.task_choice:
            return False
        if self.economic.shop_standby(pioneer, self.route(pioneer)):
            self.engaged.add(pioneer.unit_id)
            return True
        return False

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
        preferred = self.fleet_layout(kind)
        candidates.sort(key=lambda p: (p not in preferred, routes.distance(p), previous != (kind, p), p.x, p.y))
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

    def fleet_layout(self, kind):
        """Prefer front rockets sharing a safe interior seat and a separate rail seat."""
        existing = list(self.turn.weapons())
        fixed_rockets = {t.pos for t in existing if t.kind=='rocket'}
        fixed_rail = {t.pos for t in existing if t.kind=='railgun'}
        fixed_rockets |= {p for p in self.planned_towers if p not in fixed_rail}
        cells = set(ring(self.turn,1))
        station = self.turn.station(); sign = 1 if station.pos.x < self.turn.width/2 else -1
        best = None
        for a,b in combinations(sorted(cells,key=lambda q:(q.x,q.y)),2):
            if not fixed_rockets <= {a,b}:
                continue
            for rail in cells-{a,b}:
                if fixed_rail and rail not in fixed_rail:
                    continue
                fleet = {a,b,rail}
                seats = cells-fleet
                shared = set(neighbours(a)) & set(neighbours(b)) & seats
                if not shared or not (set(neighbours(rail)) & seats):
                    continue
                gate_seats = set(neighbours(self.memory.gate_pos)) & seats if self.memory.gate_pos else seats
                if not gate_seats:
                    continue
                seen = set(gate_seats)
                for _ in range(len(seats)):
                    seen |= {q for p in seen for q in neighbours(p) if q in seats}
                if not seats <= seen:
                    continue
                score = (-sign*(a.x+b.x),sum(min(distance(r.pos,q) for r in self.turn.workers())
                          for q in fleet) if self.turn.workers() else 0,a.x,a.y,b.x,b.y,rail.x,rail.y)
                if best is None or score < best[0]:
                    best = score,{a,b} if kind=='rocket' else {rail}
        return best[1] if best else set()

    def construction_accessible(self, target, tower=False):
        """Keep workers and distinct operating seats connected to the outside."""
        from collections import deque
        free = self.geo.free - self.build_targets
        # Daytime connectivity goes through the designated open gate. Closed
        # layouts instead retain internal operator/repair connectivity.
        outside = (set(ring(self.turn, 3)) if self.day < 3 else
                   {q for q in neighbours(self.memory.gate_pos) if self.base_distance(q)==1}
                   if self.memory.gate_pos else set(ring(self.turn,1))) & free
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
        return [p for p in self.walls if p not in occupied and p != self.memory.gate_pos]

    def build_wall(self, role, routes):
        missing = self.missing_walls()
        if not missing:
            return False
        stock = role.backpack.count('stone')
        if self.day >= 3 and stock <= 1 and not self.gate_wall():
            # Reserve a physical stone for the last daytime gate closure.
            return self.fetch_stone(role,routes,2)
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
            return self.fetch_stone(role, routes, 3)
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

    def fetch_stone(self, role, routes, need=1):
        """去最近的石矿采够 need 块石头(认领矿点, 避免两名工人争同一矿)。"""
        if role.backpack_full:
            return False
        mines = [p for p, kind in self.turn.zones.items()
                 if kind == 'stone' and not self.economic.blocked(p, kind)
                 and self.economic.mine_claims.get(p, 0) == 0]
        for target in sorted(mines, key=routes.distance):
            if self.enough_time(role, routes, target, max(1, need)):
                self.economic.mine_claims[target] += 1
                return self.interact(role, routes, target, 'collect', targetPos=[target.dump()])
        return False

    def upgrade_options(self):
        return self.economic.options()

    def deliver_upgrade(self,role,routes):
        self.economic.prepare()
        return self.economic.upgrade(role,routes)

    def economy(self,role,routes):
        self.economic.prepare()
        return self.economic.act(role,routes)

    @staticmethod
    def _battle_over(turn):
        """逐回合的瞬时判定: 阵前(威胁半径内)是否已经没有活机器人。"""
        from .geography import battle_over
        return battle_over(turn)

    def night_battle_over(self):
        """当前这一回合阵前是否清空(瞬时判定, 仅用于调试/测试)。"""
        return self._battle_over(self.turn)

    def night_work_mode(self):
        """夜间是否已经进入'开工'状态锁(连续阵前清空 N 回合后才为真)。"""
        return self.memory.night_mode == 'work'

    def return_home_when_idle(self):
        """夜间兜底: 没活可干又不在炮位旁的角色, 送回武器塔旁。

        没有这段时, 夜里出工后一旦"矿点被同伴占满 / 背包满 / 任务点没报价",
        角色会带着"已占岗位"标记站在原地过夜(现象就是"人一直在外面不回来")。
        """
        towers = list(self.turn.weapons())
        if not towers:
            return
        for role in self.turn.controllable():
            if str(role.unit_id) in self.commands or role.unit_id in self.engaged:
                continue
            if any(distance(role.pos, tower.pos) <= 1 for tower in towers):
                continue                      # 已经在炮位旁
            routes = self.route(role)
            seats = []
            for tower in towers:
                seats.extend(q for q in neighbours(tower.pos)
                             if q in routes.cost
                             and self.memory.blocked_goals.get((role.unit_id, q), 0)
                             <= self.turn.round_no)
            if not seats:
                continue
            stand = min(seats, key=lambda q: (routes.cost.get(q, 10**6), q.x, q.y))
            if self.move(role, routes, stand):
                self.memory.prepositioned.discard(role.unit_id)   # 交回炮位分配口径
                self.memory.event(f'role {role.unit_id}: night idle -> back to tower')

    def post_target(self, role, routes):
        """下一天的岗位: 开拓者 -> 任务点旁; 工人 -> 次日矿点的采集邻格。"""
        if role.kind == 'pioneer':
            stand = Tasks(self).next_day_post(role)
            if stand is not None and distance(role.pos, stand) <= 1:
                return role.pos          # 已经在任务点旁 -> 原地待命, 不要来回挪
            return stand
        return self.economic.next_day_mine(role, routes)

    def continue_preposition(self, role):
        """已在去岗位路上的角色继续走 —— 它们离开了炮位, 不会出现在 ready 里。"""
        if role.unit_id not in self.memory.prepositioned:
            return False
        routes = self.route(role)
        stand = self.post_target(role, routes)
        if stand is None or stand == role.pos:
            return False
        return self.move(role, routes, stand)

    def preposition(self, role):
        """需求4: 夜战结束后先去占下一天的岗位 —— 工人到次日矿点旁, 开拓者到任务点旁。"""
        if role.unit_id in self.memory.prepositioned:
            return True                      # 已经在去岗位的路上, 不再被炮位分配拉回
        if not self.night_work_mode():
            return False                     # 只有进入"开工"状态锁才出工(连续清空才切)
        station = self.turn.station()
        if station is None:
            return False
        routes = self.route(role)
        stand = self.post_target(role, routes)
        if stand is None or stand == role.pos:
            return False
        if distance(stand, station.pos) <= 2:
            return False                     # 本来就在基地旁, 不必挪
        if any(r.health > 0 and distance(r.pos, stand) <= PREPOSITION_SAFE_RADIUS
               for r in self.turn.robots):
            return False                     # 目的地附近还有机器人 -> 不冒险
        self.memory.prepositioned.add(role.unit_id)
        self.memory.event(f'role {role.unit_id}: night move to next-day post {stand}')
        return self.move(role, routes, stand)

    def assign_towers(self, day=False):
        roles = [r for r in self.turn.controllable() if str(r.unit_id) not in self.commands
                 and r.unit_id not in self.engaged
                 and r.unit_id not in self.memory.prepositioned]
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
        if not self.night_work_mode():
            self.combat_night()
            return
        # Defense owns all available operators at night. No delivery trip may
        # remove a controller; cooldown/idle turns may use items in place only.
        self.economic.prepare()
        if not self.night_work_mode():
            # 处于防守模式(含状态锁刚切回的那一回合): 先解除"已占下一天岗位",
            # 这样下面的炮位分配本回合就能把出去干活的人召回来, 不会出现无人下指令的空转
            self.memory.prepositioned.clear()
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
                elif role.kind == 'pioneer' and self.preposition(role):
                    continue
        # 需求4: 开拓者夜里先到任务点旁就位(工人不走这条: 见下, 工人统一由 Economy.act 规划)
        for role in self.turn.controllable():
            if str(role.unit_id) in self.commands or role.kind != 'pioneer':
                continue
            self.continue_preposition(role)
        # 夜战已结束时, 工人不需要再回炮位待命: 取消 assign_towers 刚发出的"回塔"移动,
        # 把这一回合让给下面的"去矿点开工"(否则夜战清场后工人还在往家走, 到天亮才采矿)
        if self.battle_over:
            seated = {role.unit_id for role, _tower in ready}
            for role in self.turn.workers():
                if role.unit_id not in seated:
                    self.commands.pop(str(role.unit_id), None)
        # 需求4: 夜战结束后夜间可以采矿/出售 —— 工人立刻在下一天要去的矿点开工
        # (夜间没打完则不进这里: 上面的防守/回塔逻辑已经占了指令)
        if self.battle_over:
            for role in self.turn.workers():
                if str(role.unit_id) in self.commands or role.unit_id in self.engaged:
                    continue
                # 只由 Economy.act 规划工人(MIN/小贩/回程都在同一份 job 里, 跨回合稳定)。
                # 之前工人同时被 next_day_mine 和 act 两套规划驱动: 前者认领矿点后 act 会
                # 跳过该矿另选一个, 两者每回合互相推翻 -> 工人在矿周围来回走却不采集。
                if self.economic.act_urgent(role, self.route(role)) or \
                        self.economic.act(role, self.route(role)):
                    # 已开工的角色按"已占下一天岗位"记账, 否则下一回合会被
                    # 炮位分配拉回基地, 与去矿点的行程来回摇摆
                    self.memory.prepositioned.add(role.unit_id)
        # 兜底: 这一回合没拿到任何指令的人(矿点被同伴占满/背包满/任务点无报价等)
        # 不能在外面干等一整夜 —— 把离塔的人送回武器塔旁
        self.return_home_when_idle()
        # (防守模式下的解除已在本回合开头完成, 这里不再重复)

    def combat_night(self):
        """Evaluate fire-only and repair-reserved alternatives before emitting actions."""
        self.memory.prepositioned.clear()
        roles = [r for r in self.turn.controllable() if str(r.unit_id) not in self.commands
                 and r.unit_id not in self.engaged]
        towers = list(self.turn.weapons())
        context = combat.combat_context(self.turn)
        delays = {t.unit_id:min((self.route(r).distance(t.pos) for r in roles),default=INF) for t in towers}
        reachable = [t for t in towers if delays[t.unit_id] < INF]
        feasible = combat.estimate_clear_feasibility(self.turn,reachable,self.memory.rocket_hits,delays)
        # Respect operator capacity when forecasting a depleted team.
        if len(roles) < len(reachable) and feasible['remaining_effective_hp']:
            feasible['clear_ratio'] *= len(roles)/max(1,len(reachable))
        imminent = any(r['imminent'] for r in context['risks'].values())
        pressure = any(r['incoming_this_round'] > 0 for r in context['risks'].values())
        mode = self.memory.choose_combat_mode(self.turn,feasible['clear_ratio'],imminent,pressure)
        context['mode'] = mode
        repairs = [None]
        for r in roles:
            if 'WallFixer' not in r.backpack:
                continue
            for wall in self.turn.walls():
                routes = self.route(r)
                seats = [q for q in neighbours(wall.pos) if q in routes.cost
                         and self.base_distance(q)<self.base_distance(wall.pos)]
                stand = min(seats,key=lambda q:(routes.cost[q],q.x,q.y),default=None)
                if stand is None:
                    continue
                eta = routes.cost[stand]
                risk = combat.predict_wall_risk(self.turn,wall,repair_eta=eta+1,threats=context['threats'])
                if combat.should_repair_wall(risk):
                    repairs.append((r,wall,eta,stand))
        cache = {}
        rail = next((t for t in towers if t.kind=='railgun'),None)
        rail_operator = min(roles,key=lambda r:(self.route(r).distance(rail.pos),
            self.memory.tower_assignments.get(r.unit_id)==rail.unit_id and -1 or 0,r.unit_id),default=None) if rail else None
        def fire(fleet):
            key = tuple(t.unit_id for t in fleet)
            if key not in cache:
                cache[key] = combat.plan_fire(self.turn,fleet,context=context)
            return cache[key]
        best = None
        for repair in repairs:
            free = [r for r in roles if not repair or r.unit_id != repair[0].unit_id]
            available = [t for t in towers if t.cooldown == 0]
            for count in range(min(len(free),len(available))+1):
                for fleet in combinations(available,count):
                    bindings = [list(zip(chosen,fleet)) for chosen in permutations(free,count)
                                if all(distance(r.pos,t.pos)==1 for r,t in zip(chosen,fleet))]
                    if not bindings:
                        continue
                    binding = min(bindings,key=lambda pairs:(sum(r==rail_operator and t.kind!='railgun' for r,t in pairs),
                                                            sum(self.memory.tower_assignments.get(r.unit_id,t.unit_id)!=t.unit_id
                                                               for r,t in pairs),tuple(r.unit_id for r,t in pairs)))
                    plan,residual = fire(fleet)
                    # A second ready rocket needs a kill-threshold reason to fire.
                    rockets = [t for t in fleet if t.kind=='rocket' and t.unit_id in plan]
                    if len(rockets)==2:
                        alternatives = [fire(tuple(t for t in fleet if t.unit_id!=rocket.unit_id))[0] for rocket in rockets]
                        single = max(alternatives,key=lambda p:combat.score_plan(self.turn,fleet,p,context))
                        if not combat.rocket_sync_value(self.turn,fleet,single,plan,context):
                            continue
                    risks = {u.unit_id:combat.predict_wall_risk(self.turn,u,residual,threats=context['threats'])
                             for u in (*self.turn.walls(),self.turn.station()) if u is not None}
                    if repair:
                        r,wall,eta,stand = repair
                        risk = combat.predict_wall_risk(self.turn,wall,residual,eta+1,context['threats'])
                        if not combat.should_repair_wall(risk):
                            continue
                        if eta == 0:
                            risk = risk.copy()
                            full_hp = (1000,1500,2000)[max(1,min(3,wall.level))-1]
                            risk['imminent'] = full_hp <= risk['damage_before_repair'] + combat.safety_margin(risk['damage_before_repair'])
                        risks[wall.unit_id] = risk
                    base_risk = risks.get(self.turn.station().unit_id,{})
                    key = (-int(base_risk.get('imminent',False)),
                           -sum(r['imminent'] for r in risks.values()),
                           int(repair is not None and repair[2]>0),
                           combat.score_plan(self.turn,fleet,plan,context),
                           -int(repair is not None), -(repair[2] if repair else 0))
                    if best is None or key > best[0]:
                        best = key,binding,plan,residual,repair
        used = set()
        if best:
            _,binding,plan,residual,repair = best
            if repair:
                r,wall,eta,stand = repair
                # Repair stands stay inside the defensive perimeter at night.
                routes = self.route(r)
                if stand == r.pos:
                    self.commands[str(r.unit_id)] = {'action':'use','name':'WallFixer','targetPos':[wall.pos.dump()]}
                else:
                    self.move(r,routes,stand)
                used.add(r.unit_id)
            for r,t in binding:
                if t.unit_id not in plan:
                    continue
                self.commands[str(t.unit_id)] = {'action':'attack','controllerId':str(r.unit_id),
                                                 'targetPos':[p.dump() for p in plan[t.unit_id]]}
                used.add(r.unit_id)
                self.memory.tower_assignments[r.unit_id] = t.unit_id
        # Free roles can use adjacent upgrades without displacing an active gun.
        serviced = {best[4][1].unit_id} if best and best[4] else set()
        for r in roles:
            if r.unit_id in used:
                continue
            if self.economic.use_consumable(r):
                used.add(r.unit_id)
                continue
            options = [o for o in self.economic.held_options(r) if o[3]!='WallFixer'
                       and o[2] not in serviced and distance(r.pos,o[4].pos)==1]
            if options:
                _,_,uid,item,target = min(options,key=lambda o:o[:3])
                self.commands[str(r.unit_id)] = {'action':'use','name':item,'targetPos':[target.pos.dump()]}
                serviced.add(uid); used.add(r.unit_id)
        self.combat_posts(roles,towers,used)

    def combat_posts(self, roles, towers, used):
        """One stable rocket-family seat, a rail seat, then an optional spare."""
        free = [r for r in roles if r.unit_id not in used and str(r.unit_id) not in self.commands]
        rockets = [t for t in towers if t.kind=='rocket']
        rail = [t for t in towers if t.kind!='rocket']
        operated = {c.get('controllerId') for c in self.commands.values() if c.get('action')=='attack'}
        active = {int(uid) for uid,c in self.commands.items() if c.get('action')=='attack'}
        targets = [t for t in rail if t.unit_id not in active]
        if rockets and not any(t.unit_id in active for t in rockets):
            targets += [min(rockets,key=lambda t:(t.cooldown,t.unit_id))]
        if not targets and free and rockets:
            targets = [min(rockets,key=lambda t:(t.cooldown,t.unit_id))]
        for t in targets:
            if not free:
                break
            r = min(free,key=lambda r:(self.route(r).distance(t.pos),
                        self.memory.tower_assignments.get(r.unit_id,t.unit_id)!=t.unit_id,r.unit_id))
            routes = self.route(r)
            seats = [q for q in neighbours(t.pos) if q in routes.cost and self.base_distance(q)<=1]
            if not seats:  # Legacy layouts outside the official inner ring.
                seats = [q for q in neighbours(t.pos) if q in routes.cost]
            shared = [q for q in seats if t.kind=='rocket' and all(distance(q,x.pos)==1 for x in rockets)]
            chosen = shared or seats
            stand = min(chosen,key=lambda q:(routes.cost[q],q.x,q.y),default=None)
            if stand is not None:
                self.move(r,routes,stand)
                self.reserved.add(stand)
                self.memory.tower_assignments[r.unit_id] = t.unit_id
                free.remove(r)
        for r in free:
            # An unassigned repair/spare role still returns to shelter. It is
            # not bound to a cooling tower and remains free for repair next turn.
            routes = self.route(r)
            seats = {q for t in towers for q in neighbours(t.pos)
                     if q in routes.cost and self.base_distance(q)<=1 and q not in self.reserved}
            stand = min(seats,key=lambda q:(routes.cost[q],q.x,q.y),default=None)
            if stand is not None:
                self.move(r,routes,stand)
                self.reserved.add(stand)
