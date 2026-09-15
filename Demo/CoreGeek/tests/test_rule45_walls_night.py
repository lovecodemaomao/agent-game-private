"""需求 4、5 的回归测试。

4. 夜战结束后: 工人立刻到下一天要去的矿点旁, 开拓者先到任务点旁
5. 石头不卖; 第3天围墙两端各加一格(10 -> 12); 第3天掉血的一级墙拆掉重建,
   掉血的二级墙用围墙修复包
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_strategy import fixture, unit
from test_tasks_economy import task_fixture
from agent.brain import Planner, decide_response, ring, wall_sites
from agent.memory import Memory
from agent.protocol import Pos, Turn, distance
from agent.economy import HP, WALL_MAINTENANCE_DAY

DAY = 130


def task_payload(desc, root='/tmp/selfEvolutionTask/x/ws_1/'):
    p = task_fixture()
    p['phaseTask'] = desc
    return p


def shops(shops_list=None):
    return [{'name': n, 'price': v} for n, v in (shops_list or [
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30),
        ('StationUpgradeVoucher1', 100), ('StationUpgradeVoucher2', 150),
        ('WallFixer', 10)])]


def build_payload(day=3, gold=200, zones=None, walls=(), roles=None, robots=(), rod=1,
                  station_health=1500, tasks=False):
    p = fixture()
    p['roundNo'] = (day - 1) * DAY + rod
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = roles if roles is not None else [
        unit(1, 'station', 10, 24, health=station_health),
        unit(2, 'worker', 9, 24), unit(3, 'worker', 12, 24),
        unit(4, 'pioneer', 10, 25),
        unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23), unit(12, 'railgun', 11, 25),
    ] + list(walls)
    p['mapInfo']['zones'] = zones if zones is not None else [
        {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
        {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
        {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
    ]
    p['weaponShopList'] = shops()
    p['robot'] = {'roles': list(robots)}
    if tasks:
        p['mapInfo']['zones'] = list(p['mapInfo']['zones']) + [
            {'pos': {'x': 20, 'y': 20}, 'neutralType': 'challengerTaskPoint1'}]
        p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 20, 'y': 20}, 'isValid': True,
                                        'coldDownRounds': 0, 'timeoutRounds': 80,
                                        'scoreReward': 50, 'goldReward': 30,
                                        'taskType': '自进化类1'}]
    return p


def robot(uid, x, y, health=100):
    return {'id': uid, 'pos': {'x': x, 'y': y}, 'health': health,
            'roleType': 'smallRobot', 'targetTeam': 'challenger'}


class StoneIsBuildingMaterialTests(unittest.TestCase):
    """需求5: 石头不卖, 只用于建墙/修墙。"""

    def test_stone_is_never_a_sale_candidate(self):
        p = build_payload(day=2)
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 12
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        planner.economic.act(worker, planner.route(worker))
        command = planner.commands.get('2', {})
        self.assertNotEqual(command.get('action'), 'sell', command)

    def test_only_ore_is_sold_even_when_stone_is_in_the_pack(self):
        # 站在小贩旁且背包里同时有石头与铜矿 -> 只卖铜矿
        p = build_payload(day=2, zones=[{'pos': {'x': 8, 'y': 24}, 'neutralType': 'vendor'}])
        p['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 25}
        p['teamOur']['roles'][1]['backpack'] = ['stone'] * 8 + ['copper'] * 4
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        planner.economic.act(worker, planner.route(worker))
        command = planner.commands['2']
        self.assertEqual(command['action'], 'sell')
        self.assertEqual(command['name'], 'copper')
        self.assertNotIn('stone', command['name'])


class StoneMorningOreNightTests(unittest.TestCase):
    """用户要求: 石头早上采, 夜晚采矿石。"""

    def payload(self, day=2, rod=10):
        # 缺墙(没有建墙) -> stone_needed 为真, 便于观察时段口径
        return build_payload(day=day, rod=rod, zones=[
            {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
            {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
            {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
        ])

    def test_morning_worker_fetches_stone_when_walls_are_missing(self):
        p = self.payload(rod=10)
        planner = Planner(Turn.load(p), p, Memory(day=2))
        worker = planner.turn.workers()[0]
        self.assertTrue(planner.economic.stone_needed(), '缺墙时需要采石')
        self.assertEqual(planner.economic.wanted_kinds(worker), ('stone',))

    def test_late_day_worker_mines_ore_instead_of_stone(self):
        p = self.payload(rod=55)                    # 过了早上窗口
        planner = Planner(Turn.load(p), p, Memory(day=2))
        worker = planner.turn.workers()[0]
        self.assertEqual(planner.economic.wanted_kinds(worker), ('iron', 'copper'),
                         '白天其余时间不再专门采石')

    def test_night_worker_mines_ore_not_stone(self):
        for rod in (75, 100, 128):
            p = self.payload(rod=rod)
            planner = Planner(Turn.load(p), p, Memory(day=2))
            worker = planner.turn.workers()[0]
            self.assertEqual(planner.economic.wanted_kinds(worker), ('iron', 'copper'),
                             f'夜间(rod{rod})应当采矿石')

    def test_day1_ore_phase_still_mines_ore_first(self):
        p = self.payload(day=1, rod=20)             # 第1天前30回合赚钱优先
        planner = Planner(Turn.load(p), p, Memory(day=1))
        worker = planner.turn.workers()[0]
        self.assertEqual(planner.economic.wanted_kinds(worker), ('iron', 'copper'))


class WallExtensionTests(unittest.TestCase):
    """需求5: 第3天白天把墙两边往后再多加一格, 变成 12 格。"""

    def test_forward_half_is_ten_cells_by_default(self):
        self.assertEqual(len(wall_sites(Turn.load(build_payload(day=2)))), 10)

    def test_day3_extends_to_twelve_cells(self):
        turn=Turn.load(build_payload(day=3))
        self.assertEqual(set(wall_sites(turn)),set(ring(turn,2)))
        self.assertEqual(len(wall_sites(turn)),20)

    def test_planner_uses_twelve_walls_from_day3(self):
        for day, expected in ((2, 10), (3, 20), (4, 20)):
            p = build_payload(day=day)
            planner = Planner(Turn.load(p), p, Memory(day=day))
            self.assertEqual(len(planner.walls), expected, f'day {day}')

    def test_extension_cells_are_built_by_workers(self):
        # 第3天缺的墙(含两端的加长格)要被列入待建清单
        p = build_payload(day=3)
        planner = Planner(Turn.load(p), p, Memory(day=3))
        self.assertEqual(len(planner.missing_walls()), 19)


class WallMaintenanceTests(unittest.TestCase):
    """需求5(现版): 一级墙不再拆掉重建 —— 带伤墙交给围墙升级券(升级即回满血);
    掉血超过 50% 的二级墙用围墙修复包。"""

    def level1_damaged_payload(self, day=3, health=400, rod=10):
        sites = wall_sites(Turn.load(build_payload(day=day)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=health)
        p = build_payload(day=day, walls=[wall], rod=rod)
        p['teamOur']['roles'][1]['pos'] = {'x': sites[0].x - 1, 'y': sites[0].y}
        return p, sites[0]

    def test_level1_damaged_wall_is_never_demolished(self):
        p, _ = self.level1_damaged_payload()
        p['teamOur']['roles'][1]['backpack'] = ['stone']
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        for _ in range(3):
            planner = Planner(Turn.load(p), p, m)
            planner.run()
            self.assertNotIn('remove', [c['action'] for c in planner.commands.values()], planner.commands)
            p['roundNo'] += 1

    def test_damaged_level1_wall_can_take_a_wall_voucher_now(self):
        # 不再有"重建"这条路 -> 带伤一级墙照常参与围墙升级券(升级会回满血)
        p, _ = self.level1_damaged_payload()
        planner = Planner(Turn.load(p), p, Memory(day=3))
        items = [o[3] for o in planner.economic.options()]
        self.assertIn('WallUpgradeVoucher1', items)

    def test_damaged_level1_wall_takes_no_fixer(self):
        # 一级墙带伤也不买修复包(修复包只给 2/3 级墙)
        p, _ = self.level1_damaged_payload()
        planner = Planner(Turn.load(p), p, Memory(day=3))
        self.assertEqual([w.unit_id for w in planner.economic.fixer_targets()], [])

    def test_slight_damage_is_ignored_before_day3(self):
        sites = wall_sites(Turn.load(build_payload(day=2)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=1200, level=2)   # 80% > 前排阈值70%
        p = build_payload(day=2, walls=[wall])
        planner = Planner(Turn.load(p), p, Memory(day=2))
        self.assertEqual(planner.economic.damaged_walls(), [])

    def test_level2_damaged_wall_is_repaired_with_fixer_on_day3(self):
        sites = wall_sites(Turn.load(build_payload(day=3)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=700, level=2)   # 2级满血1500, 掉血过半
        p = build_payload(day=3, walls=[wall])
        p['teamOur']['roles'][1]['pos'] = {'x': sites[0].x - 1, 'y': sites[0].y}
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer']
        planner = Planner(Turn.load(p), p, Memory(day=3))
        self.assertIn(40, [w.unit_id for w in planner.economic.fixer_targets()])
        planner.run()
        command = planner.commands.get('2')
        self.assertIsNotNone(command, planner.commands)
        self.assertEqual(command['action'], 'use')
        self.assertEqual(command['name'], 'WallFixer')

    def test_level2_wall_losing_less_than_half_is_left_alone(self):
        sites = wall_sites(Turn.load(build_payload(day=3)))
        wall = unit(40, 'wall', sites[0].x, sites[0].y, health=900, level=2)   # 掉血 40%
        p = build_payload(day=3, walls=[wall])
        planner = Planner(Turn.load(p), p, Memory(day=3))
        self.assertEqual(planner.economic.fixer_targets(), [])


class ShopStandbyTests(unittest.TestCase):
    """需求2: 开拓者无任务时站商店旁按计划买券, 硬性前提是白天结束前回到武器塔旁。"""

    def shop_payload(self, rod, shop_x=20, gold=250):
        """基地在 (10,24), 武器商店放在 (shop_x,24); 开拓者先站在商店旁。"""
        p = build_payload(day=1, gold=gold, rod=rod, zones=[
            {'pos': {'x': shop_x, 'y': 24}, 'neutralType': 'weaponShop'},
            {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        ])
        for role in p['teamOur']['roles']:
            if role['id'] == 4:
                role['pos'] = {'x': shop_x - 1, 'y': 24}
        return p

    def trip_length(self, payload):
        """与 shop_standby 内部一致的口径: 买一次 + 绕开待建墙格回塔 + 1 回合余量。"""
        planner = Planner(Turn.load(payload), payload, Memory(day=1))
        pioneer = next(r for r in planner.turn.alive(('pioneer',)))
        return planner.home_cost(pioneer, pioneer.pos, avoid_parking=True) + 2

    def test_stays_at_the_shop_while_the_return_still_fits(self):
        payload = self.shop_payload(rod=20)
        trip = self.trip_length(payload)
        payload['roundNo'] = 70 - trip - 1          # 富余 1 回合
        planner = Planner(Turn.load(payload), payload, Memory(day=1))
        self.assertTrue(planner.economic.shop_standby(
            next(r for r in planner.turn.alive(('pioneer',))),
            planner.route(next(r for r in planner.turn.alive(('pioneer',))))))

    def test_leaves_the_shop_when_the_return_would_not_finish_before_dark(self):
        payload = self.shop_payload(rod=20)
        trip = self.trip_length(payload)
        payload['roundNo'] = 70 - trip + 1          # 白天结束前回不到塔
        planner = Planner(Turn.load(payload), payload, Memory(day=1))
        pioneer = next(r for r in planner.turn.alive(('pioneer',)))
        self.assertFalse(planner.economic.shop_standby(pioneer, planner.route(pioneer)))
        # 交回调度后会朝武器塔移动
        planner.run()
        command = planner.commands.get('4')
        self.assertIsNotNone(command, planner.commands)
        self.assertEqual(command['action'], 'move')
        target = Pos.load(command['targetPos'][0])
        towers = [t.pos for t in planner.turn.weapons()]
        self.assertLess(min(distance(target, q) for q in towers),
                        min(distance(pioneer.pos, q) for q in towers))

    def test_pioneer_is_home_before_the_day_ends(self):
        # 从"还能站店"的最后一回合开始跑, 到第 70 回合必须在武器塔旁
        payload = self.shop_payload(rod=20)
        trip = self.trip_length(payload)
        payload['roundNo'] = 70 - trip
        memory = Memory(day=1)
        for _ in range(trip + 2):
            planner = Planner(Turn.load(payload), payload, memory)
            if (payload['roundNo'] - 1) % 130 + 1 > 70:
                break
            planner.run()
            for role in payload['teamOur']['roles']:
                command = planner.commands.get(str(role['id']))
                if command and command.get('targetPos'):
                    role['pos'] = command['targetPos'][0]
            payload['roundNo'] += 1
        pioneer = next(r for r in payload['teamOur']['roles'] if r['id'] == 4)
        towers = [(u['pos']['x'], u['pos']['y']) for u in payload['teamOur']['roles']
                  if u['roleType'] in ('rocket', 'railgun', 'gatling')]
        pos = Pos.load(pioneer['pos'])
        self.assertLessEqual(min(distance(pos, Pos(*q)) for q in towers), 1,
                             f"第70回合开拓者应已在武器塔旁, 实际 {pioneer['pos']}")

    def test_buys_plan_items_once_standing_at_the_shop(self):
        payload = self.shop_payload(rod=10)
        planner = Planner(Turn.load(payload), payload, Memory(day=1))
        self.assertTrue(planner.pioneer_standby())
        command = planner.commands.get('4')
        self.assertEqual(command['action'], 'buy', command)

    def test_no_shopping_at_night(self):
        payload = self.shop_payload(rod=85)
        planner = Planner(Turn.load(payload), payload, Memory(day=1))
        pioneer = next(r for r in planner.turn.alive(('pioneer',)))
        self.assertFalse(planner.economic.shop_standby(pioneer, planner.route(pioneer)))


class NightPrepositionTests(unittest.TestCase):
    """需求4: 夜战结束后工人去次日矿点旁, 开拓者去任务点旁。"""

    def night_payload(self, robots=(), tasks=False, rod=100, day=1, gold=0, roles=None, zones=None):
        p = build_payload(day=day, gold=gold, robots=robots, tasks=tasks, rod=rod,
                          roles=roles, zones=zones)
        return p

    def test_worker_moves_toward_next_day_mine_when_battle_is_over(self):
        p = self.night_payload(rod=100)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        stand = planner.economic.next_day_mine(worker, planner.route(worker))
        self.assertIsNotNone(stand)
        mines = [pos for pos, kind in planner.turn.zones.items() if kind in ('stone', 'iron', 'copper')]
        self.assertEqual(min(distance(stand, mine) for mine in mines), 1,
                         '预置站位必须是矿点的采集邻格')

    def test_robot_at_the_threat_radius_edge_does_not_cause_pacing(self):
        # Distance changes cannot release operators while any own-wave robot survives.
        p=self.night_payload(rod=100,robots=[robot(1,24,24)])
        m=Memory(day=3)
        for x in (24,19,24,19,24):
            p['robot']['roles']=[robot(1,x,24)]
            decide_response(p,m)
            self.assertFalse(Planner(Turn.load(p),p,m).night_work_mode())
            p['roundNo']+=1

    def test_idle_worker_returns_home_at_night_instead_of_staying_out(self):
        """回归: 夜里进入开工状态但没活可干(无矿可采)时, 工人必须回武器塔旁。

        此前这种人会带着"已占岗位"标记站在外面过夜(现象: 人一直在外面不回来)。
        """
        p = self.night_payload(rod=124, zones=[
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
            {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
        ])
        for role in p['teamOur']['roles']:
            if role['id'] == 2:
                role['pos'] = {'x': 6, 'y': 16}          # 远在外面, 附近没有任何矿
        m = Memory(day=3)
        decide_response(p, m)
        p['roundNo'] += 1
        response = decide_response(p, m)                 # 第 2 个清空回合 -> 开工状态
        self.assertTrue(Planner(Turn.load(p), p, m).night_work_mode())
        worker = next(r for r in p['teamOur']['roles'] if r['id'] == 2)
        command = response['roleCommandMap'].get('2')
        self.assertIsNotNone(command, '没活干也不能干等: 应当给出回塔的移动指令')
        self.assertEqual(command['action'], 'move')
        target = Pos.load(command['targetPos'][0])
        towers = [t.pos for t in Turn.load(p).weapons()]
        self.assertLess(min(distance(target, t) for t in towers),
                        min(distance(Pos.load(worker['pos']), t) for t in towers),
                        '应当朝武器塔移动')

    def test_idle_pioneer_returns_home_at_night_when_no_task(self):
        """回归: 夜里没有任务报价时, 开拓者不能停在任务点外面, 要回塔旁。"""
        p = task_payload('【自进化任务】暂无任务')
        p['teamOur']['playerTasks'] = []
        p['roundNo'] = 124
        for role in p['teamOur']['roles']:
            if role['id'] == 4:
                role['pos'] = {'x': 18, 'y': 16}         # 远在外面
        m = Memory(day=1)
        decide_response(p, m)
        self.assertTrue(m.task['positions'], '报价里查不到位置时要用己方任务点格兜底(不能是空列表)')
        p['roundNo'] += 1
        response = decide_response(p, m)
        command = response['roleCommandMap'].get('4')
        self.assertIsNotNone(command, '没有任务时开拓者也应当回塔, 不能停在外面')
        self.assertEqual(command['action'], 'move')

    def test_worker_stays_when_robots_are_still_attacking(self):
        p = self.night_payload(rod=100, robots=[robot(1, 9, 24)])
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        self.assertFalse(planner.night_battle_over())

    def test_distant_wandering_robot_does_not_block_night_actions(self):
        # Explicit opponent waves do not delay our work; own stragglers do.
        p=self.night_payload(rod=100,robots=[robot(9,38,2)])
        p['robot']['roles'][0]['targetTeam']='defender'
        m=Memory(day=3)
        for _ in range(2):
            decide_response(p,m); p['roundNo']+=1
        self.assertTrue(Planner(Turn.load(p),p,m).night_work_mode())
        p['robot']['roles'][0]['targetTeam']='challenger'
        decide_response(p,m)
        self.assertFalse(Planner(Turn.load(p),p,m).night_work_mode())

    def test_robot_inside_threat_radius_keeps_everyone_on_defense(self):
        p = self.night_payload(rod=100, robots=[robot(1, 12, 25)])
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        self.assertFalse(planner.night_battle_over())
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.preposition(worker), '阵前有敌时先守炮位')

    def test_worker_leaves_after_two_calm_night_rounds(self):
        # 夜战状态锁: 连续 2 回合阵前清空才出工(防止机器人在半径边缘来回导致反复出工/回塔)
        p = self.night_payload(rod=124)
        m = Memory(day=1)
        decide_response(p, m)                     # 第 1 个清空回合(去抖)
        p['roundNo'] += 1
        decide_response(p, m)                     # 第 2 个清空回合 -> 开工
        p['roundNo'] += 1
        planner = Planner(Turn.load(p), p, m)
        self.assertTrue(planner.night_work_mode())
        planner.run()
        self.assertIn(2, m.prepositioned, '连续清空后工人已被记为"已占下一天岗位"')
        self.assertIn('2', planner.commands, '开工状态应当给工人下达移动/采集指令')

    def test_no_preposition_into_a_cell_next_to_a_robot(self):
        p = self.night_payload(rod=126, robots=[robot(1, 6, 22)])
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        worker = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.preposition(worker))

    def test_prepositioned_role_is_not_pulled_back_to_a_tower(self):
        p = self.night_payload(rod=126)
        m = Memory(day=1)
        m.prepositioned.add(2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        planner.assign_towers()
        self.assertNotIn('2', planner.commands, planner.commands)

    def test_preposition_resets_on_the_next_day(self):
        p = self.night_payload(rod=126)
        m = Memory(day=1)
        m.prepositioned.add(2)
        p['roundNo'] = DAY + 1                 # 新的一天
        decide_response(p, m)
        self.assertEqual(m.prepositioned, set())

    def test_pioneer_goes_to_the_task_point_when_idle(self):
        p = self.night_payload(rod=126, tasks=True)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        pioneer = [r for r in planner.turn.alive(('pioneer',))][0]
        stand = __import__('agent.tasks', fromlist=['Tasks']).Tasks(planner).next_day_post(pioneer)
        self.assertIsNotNone(stand)
        point = Pos(20, 20)
        self.assertEqual(distance(stand, point), 1, '开拓者应站在任务点旁')

    def test_pioneer_does_not_move_while_a_task_is_active(self):
        p = self.night_payload(rod=126, tasks=True)
        m = Memory(day=1)
        m.task = {'desc': '任务', 'start': 1, 'timeout': 100, 'token': 'x', 'positions': [Pos(20, 20)],
                  'history': [], 'proposal': None, 'waiting_cmd': False, 'skill': '', 'probes': 0,
                  'probe_results': [], 'probing': False, 'result': '', 'result_seq': 0,
                  'derived_seq': 0, 'tpl_tries': 0, 'tpl_last': ''}
        planner = Planner(Turn.load(p), p, m)
        pioneer = [r for r in planner.turn.alive(('pioneer',))][0]
        self.assertIsNone(__import__('agent.tasks', fromlist=['Tasks']).Tasks(planner).next_day_post(pioneer))

    def test_everyone_walks_to_the_post_over_following_rounds(self):
        # 连续夜末回合: 工人最终应停在矿点旁(而不是被炮位分配反复拉回)
        p = self.night_payload(rod=124)
        m = Memory(day=1)
        for _ in range(16):
            response = decide_response(p, m)
            command = response['roleCommandMap'].get('2')
            if command and command['action'] == 'move':
                for role in p['teamOur']['roles']:
                    if role['id'] == 2:
                        role['pos'] = command['targetPos'][0]
            p['roundNo'] += 1
            if p['roundNo'] % DAY == 1:
                break
        worker = next(r for r in p['teamOur']['roles'] if r['id'] == 2)
        mines = [Pos.load(z['pos']) for z in p['mapInfo']['zones']
                 if z['neutralType'] in ('stone', 'iron', 'copper')]
        self.assertEqual(min(distance(Pos.load(worker['pos']), mine) for mine in mines), 1,
                         worker['pos'])


if __name__ == '__main__':
    unittest.main()
