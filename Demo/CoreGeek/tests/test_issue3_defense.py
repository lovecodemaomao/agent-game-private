"""issue #3 的回归测试：前期武器优先、围墙 <50% 立即修复、常备 2-3 个 WallFixer。

issue 要点:
  1. 取消"首日金钱>100 优先买大机器人召唤令"，前期金币优先武器升级;
  2. 每回合检测围墙血量，<50% 且背包有 WallFixer 立即使用修复（面向机器人的前排优先）;
  3. 常备 2-3 个 WallFixer。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from test_strategy import fixture, unit
from agent.brain import Planner, decide_response, wall_sites
from agent.memory import Memory
from agent.protocol import Turn, Pos, distance
from agent.economy import SUMMON_ORDER, WALL_FIXER_RESERVE


def payload(day=1, gold=75, walls=None, zones=None, hour=0):
    p = fixture()
    p['roundNo'] = (day - 1) * 130 + 1 + hour
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = [
        unit(1, 'station', 10, 24, health=1500),
        unit(2, 'worker', 9, 24),
        unit(3, 'worker', 12, 24),
        unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23), unit(12, 'railgun', 11, 25),
    ] + list(walls or [])
    p['mapInfo']['zones'] = zones if zones is not None else [
        {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
        {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
    ]
    p['weaponShopList'] = [{'name': n, 'price': v} for n, v in [
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30),
        ('WallFixer', 10), (SUMMON_ORDER, 100)]]
    return p


class WallRepairTests(unittest.TestCase):
    def test_repairs_any_level_wall_below_half_with_held_fixer(self):
        # Daytime L1 damage must not consume a repair pack; use a voucher or rebuild.
        front = wall_sites(Turn.load(payload()))[0]
        p = payload(day=2,gold=0,walls=[unit(40,'wall',front.x,front.y,health=400)])
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer','WallUpgradeVoucher1']
        p['teamOur']['roles'][1]['pos'] = {'x':front.x-1,'y':front.y}
        planner = Planner(Turn.load(p),p,Memory(day=2))
        planner.economic.prepare()
        role = planner.turn.workers()[0]
        self.assertTrue(planner.economic.upgrade(role,planner.route(role)))
        self.assertEqual(planner.commands['2']['name'],'WallUpgradeVoucher1')

    def test_healthy_wall_is_not_repaired(self):
        sites = wall_sites(Turn.load(payload()))
        front = sites[0]
        p = payload(day=2, gold=200, walls=[
            unit(40, 'wall', front.x, front.y, health=900)])      # 90% 血量
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer']
        # 夜间必须站在靠基地内侧(外侧会被机器人打)
        inner = min(((max(abs(front.x+dx-10), abs(front.y+dy-24)), (front.x+dx, front.y+dy))
                     for dx in (-1,0,1) for dy in (-1,0,1) if dx or dy))[1]
        p['teamOur']['roles'][1]['pos'] = {'x': inner[0], 'y': inner[1]}
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        self.assertNotIn('2', planner.commands)

    def test_front_wall_repaired_before_side_wall(self):
        sites = wall_sites(Turn.load(payload()))
        front, side = sites[0], sites[-1]
        p = payload(day=2, gold=200, walls=[
            unit(40, 'wall', front.x, front.y, health=400, level=3),
            unit(41, 'wall', side.x, side.y, health=400, level=3)])
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer']
        # 夜间必须站在靠基地内侧(外侧会被机器人打)
        inner = min(((max(abs(front.x+dx-10), abs(front.y+dy-24)), (front.x+dx, front.y+dy))
                     for dx in (-1,0,1) for dy in (-1,0,1) if dx or dy))[1]
        p['teamOur']['roles'][1]['pos'] = {'x': inner[0], 'y': inner[1]}
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        options = planner.economic.options()
        fixers = [o for o in options if o[3] == 'WallFixer']
        self.assertEqual(fixers[0][4].pos, front, '面向机器人的墙应先修')

    def test_low_level_wall_without_fixer_still_upgrades(self):
        # 无修复包时, 低等级受损墙用升级券（升级同时回满血）而不是买修复包
        sites = wall_sites(Turn.load(payload()))
        front = sites[0]
        p = payload(day=2, gold=200, walls=[
            unit(40, 'wall', front.x, front.y, health=400)])
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        items = [o[3] for o in planner.economic.options()]
        self.assertIn('WallUpgradeVoucher1', items)
        self.assertNotIn('WallFixer', items)

    def test_night_repairs_when_already_adjacent(self):
        # No incoming wave: a low-health wall can safely wait until daytime.
        front = wall_sites(Turn.load(payload()))[0]
        p = payload(day=1,gold=0,walls=[unit(40,'wall',front.x,front.y,health=200)])
        p['roundNo']=129
        p['teamOur']['roles'][1]['backpack']=['WallFixer']
        result=decide_response(p,Memory(day=1))['roleCommandMap']
        self.assertFalse(any(c.get('name')=='WallFixer' for c in result.values()))


class FixerReserveTests(unittest.TestCase):
    def maxed(self, gold):
        """全部满级且已建墙 -> 无券可买, 余钱才轮到储备修复包。"""
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=gold, walls=[unit(40 + i, 'wall', q.x, q.y, health=2000, level=3)
                                             for i, q in enumerate(sites[:3])])
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('rocket', 'railgun', 'station'):
                u['level'] = 3
        return p

    def quotas_done(self, m):
        """模拟当天围墙/武器配额已完成, 余钱才轮到修复包。"""
        m.day_wall_upgrades = 4
        m.day_weapon_upgrades = 2
        return m

    def test_reserves_up_to_three_fixers(self):
        p = self.maxed(150)
        m = self.quotas_done(Memory(day=2))
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        jobs = [j for j in m.jobs.values() if j.get('item') == 'WallFixer']
        self.assertTrue(jobs, m.jobs)
        self.assertTrue(jobs[0].get('keep'), '储备件买到后应留存而非立即使用')

    def test_stops_reserving_at_target(self):
        p = self.maxed(150)
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer'] * WALL_FIXER_RESERVE
        m = self.quotas_done(Memory(day=2))
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        self.assertFalse([j for j in m.jobs.values() if j.get('item') == 'WallFixer'], m.jobs)

    def test_no_fixer_reserve_until_daily_quotas_done(self):
        # 新规则: 当天的围墙/武器升级配额未完成时, 不买修复包(闲钱才买)
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=300, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        m = Memory(day=2)                     # 配额: 围墙4/武器1 均未完成
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        self.assertFalse([j for j in m.jobs.values() if j.get('item') == 'WallFixer'], m.jobs)
        self.assertTrue([j for j in m.jobs.values() if j.get('item','').startswith('Wall')]
                        or [j for j in m.jobs.values() if j.get('item','').startswith('Weapon')], m.jobs)

    def test_fixer_reserve_uses_spare_money_after_quotas(self):
        # 配额完成后, 余钱才用于修复包
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=300, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=2000, level=3)])
        m = Memory(day=2, day_wall_upgrades=4, day_weapon_upgrades=1)
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('rocket', 'railgun'):
                u['level'] = 3                 # 无武器券可买
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        self.assertTrue([j for j in m.jobs.values() if j.get('item') == 'WallFixer'], m.jobs)

    def test_reserve_never_assigned_to_worker_already_holding_fixer(self):
        # 回归: 曾把"补第2个修复包"派给已持有修复包的工人, 差事被立即弹掉
        # 造成每回合空转、采购名额被占死(200+金币却买不到任何升级券)
        p = self.maxed(200)                                       # 配额已无券可买
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer']      # worker 2 已持有
        m = self.quotas_done(Memory(day=2))
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        jobs = [j for j in m.jobs.values() if j.get('item') == 'WallFixer']
        self.assertTrue(jobs, m.jobs)
        holder = [uid for uid, j in m.jobs.items() if j.get('item') == 'WallFixer'][0]
        self.assertNotEqual(holder, 2, '不应派给已持有修复包的工人')

    def test_voucher_purchase_not_starved_by_reserve(self):
        # 修复包已备齐(3个)后, 武器券采购必须能正常安排
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=200, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        p['teamOur']['roles'][1]['backpack'] = ['WallFixer'] * WALL_FIXER_RESERVE
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        self.assertTrue([j for j in m.jobs.values() if j.get('type') == 'upgrade'], m.jobs)

    def test_reserve_keeps_budget_for_weapon_voucher(self):
        # 金币不足以同时保住武器券预算时不买修复包
        p = payload(day=2, gold=60)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        bought = [j for j in m.jobs.values() if j.get('item') == 'WallFixer']
        self.assertFalse(bought, m.jobs)


if __name__ == '__main__':
    unittest.main()


class SpendBeforeHomeTests(unittest.TestCase):
    """回家前把闲钱花完: 30 -> 围墙券+修复包; 20 -> 围墙券; 10 -> 修复包。"""

    def planner_at_dusk(self, gold, backpack=None, weapons_maxed=True):
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=gold, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        if weapons_maxed:
            for u in p['teamOur']['roles']:
                if u['roleType'] in ('rocket', 'railgun', 'gatling'):
                    u['level'] = 3          # 武器已满级 -> 闲钱才轮到围墙券/修复包
        # 站在商店(7,24)旁, 且仍有余额能在死线前回家
        p['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 24}
        if backpack:
            p['teamOur']['roles'][1]['backpack'] = list(backpack)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        return planner, role

    def test_thirty_buys_wall_voucher_first(self):
        planner, role = self.planner_at_dusk(30)
        self.assertTrue(planner.economic.spend_before_home(role, planner.route(role)))
        cmd = planner.commands['2']
        self.assertEqual(cmd['action'], 'buy')
        self.assertTrue(cmd['name'].startswith('Wall'), cmd)

    def test_twenty_buys_wall_voucher(self):
        planner, role = self.planner_at_dusk(20)
        self.assertTrue(planner.economic.spend_before_home(role, planner.route(role)))
        self.assertEqual(planner.commands['2']['name'], 'WallUpgradeVoucher1')

    def test_ten_buys_fixer(self):
        planner, role = self.planner_at_dusk(10)
        self.assertTrue(planner.economic.spend_before_home(role, planner.route(role)))
        self.assertEqual(planner.commands['2']['name'], 'WallFixer')

    def test_hundred_buys_weapon_voucher_first(self):
        planner, role = self.planner_at_dusk(100, weapons_maxed=False)
        self.assertTrue(planner.economic.spend_before_home(role, planner.route(role)))
        self.assertEqual(planner.commands['2']['name'], 'WeaponUpgradeVoucher1')

    def test_no_spend_without_affordable_item(self):
        planner, role = self.planner_at_dusk(5)
        self.assertFalse(planner.economic.spend_before_home(role, planner.route(role)))

    def test_no_wall_spend_while_weapon_upgrade_pending(self):
        # 武器配额未完成且钱不够买武器券 -> 攒钱, 不买围墙券/修复包
        planner, role = self.planner_at_dusk(30, weapons_maxed=False)
        self.assertFalse(planner.economic.spend_before_home(role, planner.route(role)))
