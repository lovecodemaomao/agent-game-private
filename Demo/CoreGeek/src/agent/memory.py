"""Per-game memory. Nothing from the judge or LLM is executed in this process."""
from dataclasses import dataclass, field


@dataclass
class Memory:
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
    task_stats: dict = field(default_factory=lambda: dict.fromkeys(
        ('accepted', 'success', 'failed', 'template_hit', 'skill_hit',
         'llm_fallback', 'rounds_total', 'llm_calls'), 0))
    task_runs: list = field(default_factory=list)
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

    def observe(self, turn, payload):
        day = (turn.round_no - 1)//130 + 1
        if self.day != day:
            self.day = day
            self.llm_used = 0
            self.news_attempts = 0
            self.day_wall_upgrades = 0
            self.day_weapon_upgrades = 0
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

    def remember(self, turn, response):
        self.last_commands = response['roleCommandMap'].copy()
        self.last_roles = {str(r.unit_id): r for r in turn.controllable()}
        self.response = response
