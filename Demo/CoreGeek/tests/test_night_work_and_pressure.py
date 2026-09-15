"""需求4 补充(夜间开工) 与需求6(围墙承伤改取向) 的回归测试。

需求4: 官方动作里 collect/sell/buy/use/acceptTask/submitAnswer/executeCmd 都没有昼夜限制
        (只有 build 限白天、attack 限黑夜), 所以"夜战结束后"工人应当直接在矿点采矿,
        开拓者应当直接在任务点继续做题, 而不是只站在旁边等天亮。
需求6: 第3天白天起, 若前一晚围墙承伤超过 50%, 当天降低武器升级优先级、提高围墙升级券
        优先级; 但基地一旦受伤, 基地升级券仍是第一优先级。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_strategy import fixture, unit
from test_rule45_walls_night import build_payload, robot
from test_tasks_economy import task_fixture
from agent.brain import Planner, decide_response, wall_sites

ENGINEER_DESC = '''进入 %s 目录，修复以下问题：
1. 创建 logs/alpha 目录
2. 修改 `config/alpha.conf` 第3行为 `port 8080`
3. 设置 bin/start.sh 可执行权限
4. 运行 ./check 验证并获取 TOKEN'''


def task_payload(desc, root='/tmp/selfEvolutionTask/x/ws_1/'):
    """带任务点的载荷: 开拓者(4)紧贴任务点(13,26), phaseTask 已生效。"""
    p = task_fixture()
    p['phaseTask'] = desc % root if '%s' in desc else desc
    return p
from agent.memory import Memory, WALL_PRESSURE_DAY, WALL_PRESSURE_RATIO
from agent.protocol import Pos, Turn, distance
from agent.tasks import Tasks


def night_payload(day=3, rod=126, robots=(), roles=None, walls=(), gold=0, zones=None):
    p = build_payload(day=day, gold=gold, robots=robots, roles=roles, walls=walls, rod=rod, zones=zones)
    return p


class NightWorkTests(unittest.TestCase):
    """需求4: 夜战结束后夜间直接开工。"""

    def test_worker_walks_to_the_mine_and_then_collects(self):
        p = night_payload(rod=124)
        m = Memory(day=3)
        collected = False
        for _ in range(10):
            response = decide_response(p, m)          # 生产路径(会推进夜战状态锁)
            command = response['roleCommandMap'].get('2')
            if command and command['action'] == 'collect':
                collected = True
                break
            if command and command['action'] == 'move':
                for role in p['teamOur']['roles']:
                    if role['id'] == 2:
                        role['pos'] = command['targetPos'][0]
            p['roundNo'] += 1
        self.assertTrue(collected, '夜间应当走到矿点并开始采集(而不是只站位)')

    def test_worker_collects_at_night_instead_of_wandering_around_the_mine(self):
        """回归: 夜里工人必须真的开始采集, 不能在矿周围来回走。

        两座矿挨得很近时, 曾经的 next_day_mine(认领矿点) 与 Economy.act(跳过已认领矿)
        两套规划每回合互相推翻, 工人就在两座矿之间打转, 一次 collect 都发不出来。
        """
        p = night_payload(rod=124, zones=[
            {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
            {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
            {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
        ])
        for role in p['teamOur']['roles']:
            if role['id'] == 2:
                role['pos'] = {'x': 7, 'y': 24}          # 已站在石矿采集邻格
        m = Memory(day=3)
        collected = False
        trail = []
        for _ in range(10):
            response = decide_response(p, m)
            command = response['roleCommandMap'].get('2')
            worker = next(r for r in p['teamOur']['roles'] if r['id'] == 2)
            trail.append((worker['pos']['x'], worker['pos']['y']))
            if command and command['action'] == 'collect':
                collected = True
                break
            self.assertIsNotNone(command, '夜间工人在矿旁时必须有指令(采集或移动)')
            if command['action'] == 'move':           # collect 的 targetPos 是矿, 不是落点
                worker['pos'] = command['targetPos'][0]
            p['roundNo'] += 1
        self.assertTrue(collected, f'夜间工人应当在矿点采集, 实际路径 {trail}')

    def test_worker_walks_straight_toward_one_mine_at_night(self):
        """回归: 夜里工人的移动方向要单调朝着矿点, 不能来回折返。"""
        p = night_payload(rod=100, zones=[
            {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
            {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
            {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
        ])
        for role in p['teamOur']['roles']:
            if role['id'] == 2:
                role['pos'] = {'x': 12, 'y': 20}         # 离矿点还有几格
        m = Memory(day=3)
        trail = []
        for _ in range(8):
            response = decide_response(p, m)
            worker = next(r for r in p['teamOur']['roles'] if r['id'] == 2)
            command = response['roleCommandMap'].get('2')
            trail.append((worker['pos']['x'], worker['pos']['y']))
            if not command or command['action'] != 'move':
                break
            worker['pos'] = command['targetPos'][0]
            p['roundNo'] += 1
        mines = [Pos(6, 24), Pos(6, 22)]
        spans = [min(distance(Pos(*q), m2) for m2 in mines) for q in trail]
        self.assertEqual(spans, sorted(spans, reverse=True),
                         f'夜间应当一路靠近矿点而不是折返, 实际 {trail}')
        self.assertEqual(len(set(trail)), len(trail), f'不应重复踩同一格(打转), 实际 {trail}')

    def test_worker_stays_on_defense_when_the_battle_is_not_over(self):
        p = night_payload(rod=100, robots=[robot(1, 9, 24)])      # 阵前有敌
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        self.assertFalse(planner.battle_over)
        self.assertEqual(planner.remaining, 0, '战斗中夜间不发放工作预算')
        planner.run()
        for uid, command in planner.commands.items():
            self.assertNotEqual(command['action'], 'collect', (uid, command))

    def test_pioneer_keeps_working_on_the_task_at_night(self):
        # 需求4: 夜战结束后开拓者不受"白天才能推进任务"的限制 —— 会继续发探测/命令,
        # 而不是被 returning 挡住干等到天亮。
        p = task_payload('【自进化任务】读文件任务')
        p['roundNo'] = 120                     # 第1天夜里(无机器人; 当夜剩余足够)
        m = Memory(day=1)
        decide_response(p, m)                  # 第1个清空回合(去抖中)
        p['roundNo'] += 1
        decide_response(p, m)                  # 第2个清空回合 -> 进入开工状态
        p['roundNo'] += 1
        first = decide_response(p, m)
        self.assertTrue(first['executeCmd'], '夜战清空后应当继续执行任务探测命令')
        self.assertIn('selfEvolutionTask', m.task['history'][-1]['command'])
        # 机器人持续回到阵前(连续 2 回合) -> 状态锁切回防守, 不再推进任务
        p['robot'] = {'roles': [robot(1, 12, 25)]}
        decide_response(p, m)
        p['roundNo'] += 1
        blocked = decide_response(p, m)
        self.assertEqual(blocked['executeCmd'], '', '持续交战时应先守阵位')

    def test_night_task_work_stops_when_robots_return(self):
        p = task_payload('请阅读 task_1_alpha.md，获取任务信息')
        p['roundNo'] = 130
        p['robot'] = {'roles': [robot(1, 12, 25)]}      # 机器人就在开拓者身边
        m = Memory(day=1)
        response = decide_response(p, m)
        self.assertEqual(response['executeCmd'], '', '战斗未结束不应执行任务命令')

    def test_can_work_reflects_day_night_and_battle(self):
        day = task_payload('任务')
        day['roundNo'] = 10
        planner = Planner(Turn.load(day), day, Memory(day=1))
        self.assertTrue(Tasks(planner).can_work())

        # 夜里刚清空: 去抖中(仍算防守) -> 不能开工; 连续清空 2 回合后才开工
        night = task_payload('任务')
        night['roundNo'] = 120
        memory = Memory(day=1)
        planner = Planner(Turn.load(night), night, memory)
        self.assertFalse(Tasks(planner).can_work())
        decide_response(night, memory)
        night['roundNo'] += 1
        decide_response(night, memory)
        night['roundNo'] += 1
        planner = Planner(Turn.load(night), night, memory)
        self.assertTrue(Tasks(planner).can_work())

        fight = task_payload('任务')
        fight['roundNo'] = 120
        fight['robot'] = {'roles': [robot(1, 10, 24)]}
        planner = Planner(Turn.load(fight), fight, Memory(day=1))
        self.assertFalse(Tasks(planner).can_work())


class WallPressureTests(unittest.TestCase):
    """需求6: 前一晚围墙承伤 > 50% -> 当天围墙券优先于武器券(基地券仍第一)。"""

    def pressure_payload(self, day=3, rod=80, health=(300, 300), level=1):
        sites = wall_sites(Turn.load(build_payload(day=day)))
        walls = [unit(40, 'wall', sites[0].x, sites[0].y, health=health[0], level=level),
                 unit(41, 'wall', sites[1].x, sites[1].y, health=health[1], level=level)]
        return build_payload(day=day, walls=walls, rod=rod)

    def run_night_then_next_day(self, night_payload, next_day, day):
        m = Memory()
        # 当天白天起点: 墙满血
        decide_response(self.pressure_payload(day=day, rod=1, health=(1000, 1000)), m)
        # 夜里(第 80 回合)被打掉大量血
        decide_response(night_payload, m)
        # 次日白天
        decide_response(self.pressure_payload(day=next_day, rod=1, health=(300, 300)), m)
        return m

    def test_heavy_night_damage_marks_high_pressure_from_day3(self):
        night = self.pressure_payload(day=2, rod=80, health=(100, 100))   # 掉血 90%
        m = self.run_night_then_next_day(night, 3, 2)
        self.assertGreater(m.wall_pressure, WALL_PRESSURE_RATIO, m.wall_pressure)
        self.assertTrue(m.wall_pressure_high)

    def test_heavy_night_damage_on_day1_does_not_count_yet(self):
        night = self.pressure_payload(day=1, rod=80, health=(100, 100))   # 掉血 90%
        m = self.run_night_then_next_day(night, 2, 1)
        self.assertFalse(m.wall_pressure_high, '第3天白天之前不计算承伤取向')

    def test_single_wall_halved_in_a_night_is_also_high_pressure(self):
        # 只有一面墙被打掉一半以上, 整体不足 50% -> 同样算"围墙承伤超过50%"
        sites = wall_sites(Turn.load(build_payload(day=2)))
        walls = [unit(40, 'wall', sites[0].x, sites[0].y, health=150),   # 单墙掉血 85%
                 unit(41, 'wall', sites[1].x, sites[1].y, health=1000),
                 unit(42, 'wall', sites[2].x, sites[2].y, health=1000)]
        night = build_payload(day=2, walls=walls, rod=80)
        m = Memory()
        decide_response(build_payload(day=2, rod=1, walls=[
            unit(40, 'wall', sites[0].x, sites[0].y, health=1000),
            unit(41, 'wall', sites[1].x, sites[1].y, health=1000),
            unit(42, 'wall', sites[2].x, sites[2].y, health=1000)]), m)
        decide_response(night, m)
        decide_response(build_payload(day=3, rod=1, walls=[
            unit(40, 'wall', sites[0].x, sites[0].y, health=150),
            unit(41, 'wall', sites[1].x, sites[1].y, health=1000),
            unit(42, 'wall', sites[2].x, sites[2].y, health=1000)]), m)
        self.assertGreater(m.wall_worst, WALL_PRESSURE_RATIO, m.wall_worst)
        self.assertLessEqual(m.wall_pressure, WALL_PRESSURE_RATIO, m.wall_pressure)
        self.assertTrue(m.wall_pressure_high)

    def test_light_night_damage_is_not_high_pressure(self):
        night = self.pressure_payload(day=2, rod=80, health=(950, 950))
        m = self.run_night_then_next_day(night, 3, 2)
        self.assertLessEqual(m.wall_pressure, WALL_PRESSURE_RATIO, m.wall_pressure)
        self.assertFalse(m.wall_pressure_high)

    def test_wall_voucher_outranks_weapon_when_pressure_is_high(self):
        sites = wall_sites(Turn.load(build_payload(day=3)))
        walls = [unit(40, 'wall', sites[0].x, sites[0].y, health=1000),
                 unit(41, 'wall', sites[1].x, sites[1].y, health=1000)]
        p = build_payload(day=3, walls=walls, rod=1)
        m = Memory(day=3)
        m.wall_pressure_high = True
        planner = Planner(Turn.load(p), p, m)
        first = planner.economic.options()[0][3]
        self.assertTrue(first.startswith('Wall'), first)

    def test_day1_weapon_voucher_still_first_without_pressure(self):
        # 第1天(无围墙升级配额)且无承伤压力 -> 仍是武器券先行
        sites = wall_sites(Turn.load(build_payload(day=1)))
        walls = [unit(40, 'wall', sites[0].x, sites[0].y, health=1000)]
        p = build_payload(day=1, walls=walls, rod=1)
        m = Memory(day=1)
        m.wall_pressure_high = False
        planner = Planner(Turn.load(p), p, m)
        first = planner.economic.options()[0][3]
        self.assertTrue(first.startswith('Weapon'), first)

    def test_day2_weapon_voucher_first_without_pressure(self):
        # 用户口径: 没有"前夜承伤 > 80%"时一律武器优先
        sites = wall_sites(Turn.load(build_payload(day=2)))
        walls = [unit(40, 'wall', sites[0].x, sites[0].y, health=1000)]
        p = build_payload(day=2, walls=walls, rod=1)
        m = Memory(day=2)
        m.wall_pressure_high = False
        planner = Planner(Turn.load(p), p, m)
        first = planner.economic.options()[0][3]
        self.assertTrue(first.startswith('Weapon'), first)

    def test_damaged_station_still_outranks_everything(self):
        sites = wall_sites(Turn.load(build_payload(day=3)))
        walls = [unit(40, 'wall', sites[0].x, sites[0].y, health=1000)]
        p = build_payload(day=3, walls=walls, rod=1, station_health=800)
        m = Memory(day=3)
        m.wall_pressure_high = True
        planner = Planner(Turn.load(p), p, m)
        self.assertEqual(planner.economic.options()[0][3], 'StationUpgradeVoucher1')

    def test_high_pressure_releases_the_weapon_reserve(self):
        # 没有承伤压力时: 保留金币给武器券, 不买围墙券(第1、2天同理)
        sites = wall_sites(Turn.load(build_payload(day=1)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=1000)
        for day in (1, 2):
            payload = build_payload(day=day, gold=40, walls=[wall], rod=1)
            memory = Memory(day=day)
            Planner(Turn.load(payload), payload, memory).economic.prepare()
            jobs = [j for j in memory.jobs.values() if j.get('item', '').startswith('Wall')]
            self.assertFalse(jobs, f'第{day}天无承伤压力时应保留金币给武器券')

        # 前夜围墙承伤超过 80%: 当天先补围墙升级券
        payload = build_payload(day=3, gold=40, walls=[wall], rod=1)
        memory = Memory(day=3)
        memory.wall_pressure_high = True
        Planner(Turn.load(payload), payload, memory).economic.prepare()
        jobs = [j for j in memory.jobs.values() if j.get('item', '').startswith('Wall')]
        self.assertTrue(jobs, '承伤超过阈值时应当直接买围墙升级券')

    def test_pioneer_standby_buys_wall_voucher_under_pressure(self):
        p = build_payload(day=3, gold=60, rod=1,
                          zones=[{'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
                                 {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'}])
        sites = wall_sites(Turn.load(p))
        p['teamOur']['roles'].append(unit(40, 'wall', sites[0].x, sites[0].y, health=1000))
        p['teamOur']['roles'][3]['pos'] = {'x': 7, 'y': 25}      # 开拓者站在商店旁
        memory = Memory(day=3)
        memory.wall_pressure_high = True
        planner = Planner(Turn.load(p), p, memory)
        self.assertTrue(planner.pioneer_standby())
        command = planner.commands.get('4')
        self.assertEqual(command['action'], 'buy', command)
        self.assertEqual(command['name'], 'WallUpgradeVoucher1')


if __name__ == '__main__':
    unittest.main()
