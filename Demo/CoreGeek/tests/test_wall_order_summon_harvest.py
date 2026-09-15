"""三项改进的回归测试。

1. 围墙升级优先朝向机器人的（迎敌半圈由前到后排序）
2. 第一天金钱超过 100 时，先于升级券购买大机器人召唤令（买到即用）
3. 白天返程时在家附近顺手采矿，但保证入夜前回到武器旁
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from test_strategy import fixture, unit
from agent.brain import Planner, wall_sites
from agent.memory import Memory
from agent.protocol import Turn, Pos, distance
from agent.economy import SUMMON_ORDER, SUMMON_ORDER_TRIGGER


def base_payload(day=1, gold=75, zones=None, walls=None):
    p = fixture()
    p['roundNo'] = (day - 1) * 130 + 1
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = [
        unit(1, 'station', 10, 24),
        unit(2, 'worker', 9, 24),
        unit(3, 'worker', 12, 24),
        unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23), unit(12, 'railgun', 11, 25),
    ] + list(walls or [])
    p['mapInfo']['zones'] = zones if zones is not None else [
        {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
        {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
        {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
    ]
    p['weaponShopList'] = [{'name': n, 'price': v} for n, v in [
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30), ('WallFixer', 10),
        (SUMMON_ORDER, 100)]]
    return p


class WallFacingOrderTests(unittest.TestCase):
    def test_front_walls_upgrade_before_side_walls(self):
        sites = wall_sites(Turn.load(base_payload()))
        self.assertGreater(len(sites), 3)
        walls = [unit(40 + i, 'wall', q.x, q.y) for i, q in enumerate(sites)]
        p = base_payload(day=2, gold=400, walls=walls)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        options = planner.economic.options()
        wall_options = [o for o in options if o[3].startswith('Wall')]
        self.assertEqual(wall_options[0][4].pos, sites[0], '最朝向机器人的墙应最先升级')
        # 排序应与迎敌弧线顺序一致（前排在前）
        order = [o[4].pos for o in wall_options]
        ranks = [sites.index(q) for q in order]
        self.assertEqual(ranks, sorted(ranks), ranks)

    def test_non_forward_wall_ranks_last(self):
        sites = wall_sites(Turn.load(base_payload()))
        rear = unit(99, 'wall', 6, 24)      # 基地后方（非迎敌半圈）；unit() 返回原始字典
        walls = [unit(40 + i, 'wall', q.x, q.y) for i, q in enumerate(sites)] + [rear]
        p = base_payload(day=2, gold=400, walls=walls)
        planner = Planner(Turn.load(p), p, Memory(day=2))
        wall_options = [o for o in planner.economic.options() if o[3].startswith('Wall')]
        self.assertEqual(wall_options[-1][4].pos, Pos(6, 24))


class SummonOrderTests(unittest.TestCase):
    def test_no_summon_order_on_day1_even_with_rich_gold(self):
        # issue #3: 取消"首日金钱>100 优先买大机器人召唤令"的逻辑
        p = base_payload(day=1, gold=300)
        p['roundNo'] = 40
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        summons = [j for j in m.jobs.values() if j.get('item') == SUMMON_ORDER]
        self.assertFalse(summons, m.jobs)

    def test_summon_order_only_after_weapons_and_walls_secured(self):
        # 武器未满2级时: 不买召唤令
        p = base_payload(day=3, gold=300)
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        self.assertFalse([j for j in m.jobs.values() if j.get('item') == SUMMON_ORDER], m.jobs)
        # 武器全 2 级 + 围墙阶段完成 -> 才允许采购
        sites = wall_sites(Turn.load(p))
        sites = [q for q in sites if q.x >= 10]  # an open rear corridor for shop access
        roles = [u for u in p['teamOur']['roles'] if u['roleType'] in ('rocket', 'railgun')]
        for u in roles:
            u['level'] = 2
        p['teamOur']['roles'] += [unit(40 + i, 'wall', q.x, q.y, health=1000, level=2)
                                  for i, q in enumerate(sites)]
        m2 = Memory(day=3)
        planner2 = Planner(Turn.load(p), p, m2)
        planner2.economic.prepare()
        self.assertTrue([j for j in m2.jobs.values() if j.get('item') == SUMMON_ORDER]
                        or any(j.get('item', '').startswith('Wall') for j in m2.jobs.values()),
                        m2.jobs)

    def test_no_summon_order_when_gold_below_trigger(self):
        p = base_payload(day=3, gold=SUMMON_ORDER_TRIGGER)
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('rocket', 'railgun'):
                u['level'] = 2
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        self.assertFalse([j for j in m.jobs.values() if j.get('item') == SUMMON_ORDER], m.jobs)

    def test_courier_buys_then_uses_order(self):
        p = base_payload(day=1, gold=120)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        p['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 24}   # 站在商店(7,24)旁
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        m.jobs[role.unit_id] = {'type': 'order', 'item': SUMMON_ORDER, 'price': 100,
                                'shop': Pos(7, 24)}
        routes = planner.route(role)
        self.assertTrue(planner.economic.order(role, routes))
        self.assertEqual(planner.commands['2']['action'], 'buy')
        self.assertEqual(planner.commands['2']['name'], SUMMON_ORDER)
        self.assertEqual(planner.gold, 20)
        # 拿到手后立即使用（使用不需要目标位置）
        p2 = base_payload(day=1, gold=20)
        p2['teamOur']['roles'][1]['backpack'] = [SUMMON_ORDER]
        m2 = Memory(day=1)
        planner2 = Planner(Turn.load(p2), p2, m2)
        m2.jobs[2] = {'type': 'order', 'item': SUMMON_ORDER, 'price': 100, 'shop': Pos(7, 24)}
        role2 = [w for w in planner2.turn.workers() if w.unit_id == 2][0]
        self.assertTrue(planner2.economic.order(role2, planner2.route(role2)))
        self.assertEqual(planner2.commands['2'], {'action': 'use', 'name': SUMMON_ORDER})
        self.assertTrue(m2.summon_order_done)


class HarvestOnTheWayHomeTests(unittest.TestCase):
    def payload_with_home_mine(self, near=True):
        mine_pos = {'x': 8, 'y': 25} if near else {'x': 20, 'y': 10}
        p = base_payload(day=1, gold=75, zones=[
            {'pos': mine_pos, 'neutralType': 'copper'},
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
            {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
        ])
        p['roundNo'] = 70                      # 死线(第75回合)前夕: remaining=6
        p['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 26}   # 工人已在矿(8,25)旁
        return p, Pos.load(mine_pos)

    def test_harvests_nearby_home_mine_on_the_way_back(self):
        p, mine = self.payload_with_home_mine(near=True)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertLessEqual(planner.remaining, planner.home_cost(role, role.pos) + 5)
        self.assertTrue(planner.economic.harvest_near_home(role, planner.route(role)))
        self.assertEqual(planner.commands['2']['action'], 'collect')
        self.assertEqual(Pos.load(planner.commands['2']['targetPos'][0]), mine)

    def test_no_harvest_for_mine_far_off_the_way_home(self):
        # 远离基地且明显绕路的矿: 不顺手, 不采
        p, _ = self.payload_with_home_mine(near=False)
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.economic.harvest_near_home(role, planner.route(role)))

    def test_harvests_mine_on_the_way_home_even_if_far_from_base(self):
        # 矿离基地较远(>4格)但在返程路线上(绕路<=2回合) -> 顺手采
        p = base_payload(day=1, gold=75, zones=[
            {'pos': {'x': 10, 'y': 16}, 'neutralType': 'iron'},   # 距基地8格, 正对返程方向
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        ])
        p['roundNo'] = 62                    # remaining=13: 够 采一次+回家+缓冲
        p['teamOur']['roles'][1]['pos'] = {'x': 10, 'y': 17}      # 就在矿旁
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertGreater(distance(Pos(10, 16), Pos(10, 24)), 4)
        self.assertTrue(planner.economic.harvest_near_home(role, planner.route(role)))
        self.assertEqual(planner.commands['2']['action'], 'collect')

    def test_no_harvest_when_time_would_run_out(self):
        p, _ = self.payload_with_home_mine(near=True)
        p['roundNo'] = 74                      # remaining=2, 已无余量顺手采
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        self.assertFalse(planner.economic.harvest_near_home(role, planner.route(role)))


if __name__ == '__main__':
    unittest.main()
