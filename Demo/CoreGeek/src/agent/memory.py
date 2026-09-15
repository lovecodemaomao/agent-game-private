"""Per-game memory. Nothing from the judge or LLM is executed in this process."""
from dataclasses import dataclass, field

NIGHT_MODE_CALM_ROUNDS = 2     # 连续 2 回合阵前清空 -> 夜间进入"开工"模式
NIGHT_MODE_HOT_ROUNDS = 2      # 连续 2 回合阵前有敌 -> 夜间回到"防守"模式
WALL_PRESSURE_DAY = 3          # 需求6: 第3天白天起开始计算围墙承伤
WALL_PRESSURE_RATIO = 0.8      # 用户口径: 前夜围墙承伤超过该比例 -> 当天降武器升级、升围墙券


@dataclass
class Memory:
    combat_mode: str = 'KILL_ALL'
    combat_day: int = 0
    combat_check_round: int = 0
    clear_fail_rounds: int = 0
    combat_switch_round: int | None = None
    rocket_hits: list = field(default_factory=list)
    pending_rocket_volley: dict | None = None
    gate_pos: object = None
    gate_state: str = 'open'
    gate_keeper: int | None = None
    wall_rebuilds: dict = field(default_factory=dict)
    defense_breached: bool = False
    night_breach: bool = False
    night_station_hit: bool = False
    previous_station_hp: int | None = None
    round_no: int = 0
    day: int = 0
    llm_used: int = 0
    pending_llm: dict | None = None
    news: list = field(default_factory=list)
    news_version: int = 0
    analysed_news: int = -1
    news_attempts: int = 0
    news_advice: dict = field(default_factory=dict)
    treasure: dict | None = None
    treasure_attempts: set = field(default_factory=set)
    treasure_done: bool = False
    task: dict | None = None
    task_choice: dict | None = None
    skills: list = field(default_factory=list)
    jobs: dict = field(default_factory=dict)
    last_commands: dict = field(default_factory=dict)
    last_roles: dict = field(default_factory=dict)
    mine_used: dict = field(default_factory=dict)
    mine_blocked_until: dict = field(default_factory=dict)
    mining_roles: dict = field(default_factory=dict)   # 矿工分工: {'stone': unit_id}
    summon_order_done: bool = False                    # 召唤令是否已购买并使用
    day_wall_upgrades: int = 0                         # 当天已完成的围墙升级数
    day_weapon_upgrades: int = 0                       # 当天已完成的武器升级数
    levels: dict = field(default_factory=dict)         # 建筑等级快照: {unit_id: level}
    station_hit: bool = False                          # 基地是否受过伤(需求3: 触发基地升级券)
    day_start_gold: int = 0                            # 当天开始时的金币(基地券档位判断)
    prepositioned: set = field(default_factory=set)    # 需求4: 夜间已去下一天岗位的角色
    night_mode: str = 'defend'                         # 夜战状态锁: 'defend' / 'work'
    night_hot: int = 0                                 # 连续"阵前有敌"回合数
    night_calm: int = 0                                # 连续"阵前清空"回合数
    wall_hp: dict = field(default_factory=dict)        # 需求6: {uid: (health, max_hp)} 逐回合核对承伤
    night_wall_damage: float = 0.0                     # 需求6: 本夜围墙累计掉血
    night_wall_total: float = 0.0                      # 需求6: 本夜围墙累计最大血量(含被打掉的)
    wall_pressure: float = 0.0                         # 需求6: 昨夜承伤比例
    wall_pressure_high: bool = False                   # 需求6: 昨夜承伤 > 50%
    wall_hp_prev: dict = field(default_factory=dict)   # 需求6: 上一回合 {uid: (health, max)}
    night_wall_max: dict = field(default_factory=dict)  # 需求6: {uid: max_hp} 今夜出现过的墙
    night_wall_worst: float = 0.0                      # 需求6: 今夜单面墙最大掉血比例
    wall_worst: float = 0.0                            # 需求6: 昨夜单面墙最大掉血比例
    day_upgrades: dict = field(default_factory=dict)   # 每日已完成升级: {day: {'wall': n, 'weapon': n}}
    previous_mines: dict = field(default_factory=dict)
    failed_steps: dict = field(default_factory=dict)
    movement: dict = field(default_factory=dict)       # uid -> last requested destination
    movement_history: dict = field(default_factory=dict)
    blocked_goals: dict = field(default_factory=dict)  # (uid, destination) -> retry round
    blocked_deliveries: dict = field(default_factory=dict)
    tower_assignments: dict = field(default_factory=dict)
    construction_jobs: dict = field(default_factory=dict)
    response: dict | None = None
    trace: list = field(default_factory=list)

    def event(self, text):
        self.trace.append(text)
        self.trace[:] = self.trace[-30:]

    def choose_combat_mode(self, turn, clear_ratio, imminent, pressure=False):
        from .fire_control import KILL_ALL_MODE, SURVIVAL_MODE, CLEAR_SAFE_RATIO, CLEAR_FAIL_RATIO
        day = (turn.round_no-1)//130+1
        if self.combat_day != day:
            self.combat_day = day
            self.combat_mode = KILL_ALL_MODE
            self.clear_fail_rounds = 0
            self.combat_check_round = 0
            self.combat_switch_round = None
            self.rocket_hits.clear()
        if self.combat_check_round == turn.round_no:
            return self.combat_mode
        consecutive = self.combat_check_round == turn.round_no-1
        self.combat_check_round = turn.round_no
        self.clear_fail_rounds = (self.clear_fail_rounds if consecutive else 0)+1 if clear_ratio < CLEAR_FAIL_RATIO else 0
        if self.combat_mode != SURVIVAL_MODE and (imminent or self.clear_fail_rounds >= 2
                or (clear_ratio < CLEAR_SAFE_RATIO and pressure)):
            self.combat_mode = SURVIVAL_MODE
            self.combat_switch_round = turn.round_no
            self.event(f'combat -> SURVIVAL: clear_ratio={clear_ratio:.2f}, imminent={imminent}')
        return self.combat_mode

    def observe(self, turn, payload):
        self.observe_rocket_volley(turn,payload)
        day = (turn.round_no - 1)//130 + 1
        if self.day != day:
            # The first dawn observation includes the final night's settlement.
            if self.round_no and (self.round_no-1)%130 >= 70:
                alive_walls = {w.unit_id for w in turn.walls()}
                self.night_breach |= bool(set(self.wall_hp)-alive_walls)
                base = turn.station()
                self.night_station_hit |= (base is not None and self.previous_station_hp is not None
                                            and base.health < self.previous_station_hp)
            self.defense_breached = self.night_breach and self.night_station_hit
            self.night_breach = self.night_station_hit = False
            self.combat_day = day
            self.combat_mode = 'KILL_ALL'
            self.combat_check_round = self.clear_fail_rounds = 0
            self.combat_switch_round = None
            self.rocket_hits.clear()
            self.day = day
            self.llm_used = 0
            self.news_attempts = 0
            self.day_wall_upgrades = 0
            self.day_weapon_upgrades = 0
            self.day_start_gold = turn.gold          # 需求3: "当天白天开始时的金币"
            self.prepositioned.clear()               # 需求4: 新的一天重新就位防守
            self.night_mode = 'defend'               # 夜战状态锁回到"防守"
            self.night_hot = 0
            self.night_calm = 0
            # 需求6: 第3天起, 用"前一晚围墙承伤是否超过 50%"决定当天券的取向
            total = sum(self.night_wall_max.values())
            self.wall_pressure = (self.night_wall_damage/total) if total > 0 else 0.0
            self.wall_worst = self.night_wall_worst
            # "围墙承伤超过 50%" 的两种读法都算吃紧:
            #   整体 —— 整条防线一夜掉血超过其总血量的一半;
            #   单墙 —— 有任意一面墙一夜被打掉一半以上的血。
            self.wall_pressure_high = bool(
                day >= WALL_PRESSURE_DAY
                and (self.wall_pressure > WALL_PRESSURE_RATIO
                     or self.wall_worst > WALL_PRESSURE_RATIO))
            self.night_wall_damage = 0.0
            self.night_wall_worst = 0.0
            self.night_wall_max = {}
        # 需求4: 夜战状态锁 —— 用连续回合数去抖, 避免机器人贴着威胁半径来回时
        # "出工/回塔"每回合翻转(实测会让工人在矿与塔之间来回踱步、既不采矿也不防守)
        if not turn.is_day:
            from .fire_control import our_target
            if not any(r.health > 0 and our_target(turn,r) for r in turn.robots):
                self.night_calm += 1
                self.night_hot = 0
            else:
                self.night_hot += 1
                self.night_calm = 0
            if self.night_calm >= NIGHT_MODE_CALM_ROUNDS:
                self.night_mode = 'work'
            elif self.night_hot:
                self.night_mode = 'defend'
        # 需求6: 累计夜间围墙承伤(掉血 + 被打掉时的剩余血量); 白天我方 remove/重建不计
        self._track_wall_damage(turn)
        # 基地受伤是永久状态: 只要掉过血就一直记着, 用来触发基地升级券
        station = turn.station()
        if station is not None:
            if not turn.is_day and self.previous_station_hp is not None and station.health < self.previous_station_hp:
                self.night_station_hit = True
            self.previous_station_hp = station.health
        if station is not None and station.health > 0:
            from .economy import HP
            level = max(1, min(3, station.level))
            if station.health < HP['station'][level-1]:
                self.station_hit = True
        if any(e.get('errorCode') == 5 for e in payload.get('errors', [])):
            self.llm_used = 3
        roles = {str(r.unit_id): r for r in turn.controllable()}
        for cache in (self.blocked_goals, self.blocked_deliveries):
            for key, until in list(cache.items()):
                if until <= turn.round_no or str(key[0]) not in roles:
                    cache.pop(key, None)
        results = payload.get('lastRoundRoleActionResults') or {}
        mines = {p: k for p, k in turn.zones.items() if k in ('stone','iron','copper')}
        for p in list(self.mine_used):
            if mines.get(p) != self.previous_mines.get(p):
                self.mine_used.pop(p, None)
                self.mine_blocked_until.pop(p, None)
        if self.round_no == turn.round_no-1:
            from .protocol import Pos
            for uid, cmd in self.last_commands.items():
                role = roles.get(uid)
                before = self.last_roles.get(uid)
                if role is None or before is None:
                    continue
                failed = results.get(uid, results.get(int(uid))) is False
                if cmd['action'] != 'move':
                    self.movement_history.pop(role.unit_id, None)
                if cmd['action'] == 'move' and (failed or role.pos == before.pos):
                    target = Pos.load(cmd['targetPos'][0])
                    self.failed_steps[role.unit_id] = (target, turn.round_no+2)
                if cmd['action'] == 'move' and role.unit_id in self.movement:
                    goal = self.movement[role.unit_id]
                    old_goal, trail = self.movement_history.get(role.unit_id, (goal, [before.pos]))
                    trail = (trail if old_goal == goal else [before.pos]) + [role.pos]
                    trail = trail[-5:]
                    self.movement_history[role.unit_id] = (goal, trail)
                    stuck = len(trail) >= 4 and len(set(trail[-4:])) == 1
                    cycling = (len(trail) == 5 and trail[0] == trail[2] == trail[4]
                               and trail[1] == trail[3])
                    if stuck or cycling:
                        self.blocked_goals[(role.unit_id, goal)] = turn.round_no + 8
                        job = self.jobs.get(role.unit_id, {})
                        if job.get('type') == 'upgrade':
                            self.blocked_deliveries[(role.unit_id, job['unit'])] = turn.round_no + 8
                        self.jobs.pop(role.unit_id, None)
                        self.tower_assignments.pop(role.unit_id, None)
                        self.construction_jobs.pop(role.unit_id, None)
                        self.movement_history.pop(role.unit_id, None)
                        self.event(f'role {uid}: no movement progress; choose another approach')
                if cmd['action'] == 'collect':
                    target = Pos.load(cmd['targetPos'][0])
                    kind = self.previous_mines.get(target)
                    gained = kind and role.backpack.count(kind) > before.backpack.count(kind)
                    if gained:
                        self.mine_used[target] = self.mine_used.get(target,0)+1
                        job = self.jobs.get(role.unit_id, {})
                        if job.get('type') == 'mine' and job.get('target') == target:
                            job['left'] = max(0, job['left']-1)
                    elif failed or kind:
                        self.mine_blocked_until[target] = turn.round_no+8
                        self.jobs.pop(role.unit_id, None)
                        self.event(f'mine unavailable at {target}; retry after 8 rounds')
                job = self.jobs.get(role.unit_id)
                if job and failed:
                    job['failures'] = job.get('failures',0)+1
                    if job['failures'] >= 3:
                        if job.get('type') == 'upgrade':
                            self.blocked_deliveries[(role.unit_id, job['unit'])] = turn.round_no + 8
                        self.jobs.pop(role.unit_id, None)
                        self.event(f'worker {uid}: replan after 3 failed actions')
        # 统计"当天完成了多少次围墙/武器升级"（用于每日必完成配额）
        for unit in turn.ours:
            key = 'wall' if unit.kind == 'wall' else ('weapon' if unit.kind in
                   ('gatling','railgun','rocket') else None)
            if key is None:
                continue
            before = self.levels.get(unit.unit_id)
            self.levels[unit.unit_id] = max(1, min(3, unit.level))
            if before is not None and unit.level > before:
                bucket = self.day_upgrades.setdefault(day, {'wall': 0, 'weapon': 0})
                bucket[key] += unit.level - before
                if key == 'wall':
                    self.day_wall_upgrades += unit.level - before
                else:
                    self.day_weapon_upgrades += unit.level - before
        self.previous_mines = mines
        for uid in list(self.jobs):
            if str(uid) not in roles:
                self.jobs.pop(uid,None)
        news = payload.get('worldNews') or {}
        entry = {'day': day, 'officialNews': str(news.get('officialNews','')),
                 'folkLegends': str(news.get('folkLegends',''))}
        if (entry['officialNews'] or entry['folkLegends']) and (not self.news or self.news[-1] != entry):
            self.news.append(entry)
            self.news[:] = self.news[-20:]
            self.news_version += 1
        self.round_no = turn.round_no

    def observe_rocket_volley(self, turn, payload):
        """Only credit confirmed, isolated rocket damage in consecutive frames.

        Action legality alone does not prove a hit. Require the observed HP
        loss to equal our immutable ballistic prediction, and reject targets
        shared with another weapon or enemy weapon in range. Missing units
        are not attributed: disappearance alone does not establish our kill.
        """
        from .fire_control import HIT_HISTORY_SIZE
        pending = self.pending_rocket_volley
        self.pending_rocket_volley = None
        if not pending or turn.round_no != pending['round']+1:
            return
        results = payload.get('lastRoundRoleActionResults') or {}
        if not all(results.get(str(uid),results.get(uid)) is True for uid in pending['towers']):
            return
        robots = {r.robot_id:r for r in turn.robots}
        total = 0
        observed = False
        for uid,(hp,damage) in pending['targets'].items():
            robot = robots.get(uid)
            if robot is None:
                continue
            loss = hp-max(0,robot.health)
            if loss == min(hp,damage):
                total += loss
                observed = True
        if observed:
            self.rocket_hits.append(total/pending['missiles'])
            self.rocket_hits[:] = self.rocket_hits[-HIT_HISTORY_SIZE:]

    def _track_wall_damage(self, turn):
        """需求6: 只在夜间统计围墙承伤 —— 掉血量 + 被打掉墙的剩余血量。

        分母是"今夜出现过的墙"的最大血量之和(逐回合取最大, 日切时结算),
        白天我方 remove/重建 换的是新单位ID, 不计入承伤。
        """
        from .economy import HP
        seen = {}
        for unit in turn.walls():
            level = max(1, min(3, unit.level))
            seen[unit.unit_id] = (unit.health, HP['wall'][level-1])
        self.wall_hp_prev = self.wall_hp
        self.wall_hp = seen
        if turn.is_day:
            return
        for uid, (health, maximum) in seen.items():
            self.night_wall_max[uid] = max(self.night_wall_max.get(uid, 0), maximum)
            before = self.wall_hp_prev.get(uid)
            if before is not None and health < before[0]:
                lost = before[0]-health
                self.night_wall_damage += lost
                if maximum > 0:
                    self.night_wall_worst = max(self.night_wall_worst, lost/maximum)
        for uid, (health, _maximum) in self.wall_hp_prev.items():
            if uid not in seen:
                # 夜里消失 = 被机器人打掉: 剩余血量计入承伤
                self.night_wall_damage += health
                self.night_breach = True

    def remember(self, turn, response):
        from .fire_control import raw_damage, our_target
        from .protocol import Pos, distance
        rockets, other, towers, missiles = {}, set(), [], 0
        for tower in turn.weapons():
            cmd = response['roleCommandMap'].get(str(tower.unit_id),{})
            if cmd.get('action') != 'attack':
                continue
            if tower.kind == 'rocket':
                towers.append(tower.unit_id)
                missiles += len(cmd['targetPos'])
            for point in cmd['targetPos']:
                for uid,damage in raw_damage(turn,tower,Pos.load(point)).items():
                    if tower.kind == 'rocket':
                        rockets[uid] = rockets.get(uid,0)+damage
                    else:
                        other.add(uid)
        targets = {r.robot_id:(r.health,rockets[r.robot_id]) for r in turn.robots
                   if r.robot_id in rockets and r.robot_id not in other and our_target(turn,r)
                   and not any(e.kind in ('rocket','railgun','gatling') and e.health>0
                               and distance(e.pos,r.pos)<=e.range_of_attack()+1 for e in turn.enemies)}
        self.pending_rocket_volley = ({'round':turn.round_no,'towers':towers,
                                      'missiles':missiles,'targets':targets} if missiles else None)
        self.last_commands = response['roleCommandMap'].copy()
        self.last_roles = {str(r.unit_id): r for r in turn.controllable()}
        self.response = response
