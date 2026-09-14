"""issue #4 相关改进的回归测试。

1. 前期两名工人一起把半圈围墙搭好, 搭好后一起采矿
2. 采集/购买批量: 一趟采够、一次买够
3. 兜底: 撑过第3夜后基地<1000血 -> 第4天优先买基地升级券
4. 任务效率: 同类任务直接复用已验证的 SOP(零探测、零 LLM)
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
from agent.economy import STATION_FALLBACK_HP, STATION_FALLBACK_DAY
from agent.tasks import Tasks


def payload(day=1, gold=75, walls=None, zones=None, station_health=1500):
    p = fixture()
    p['roundNo'] = (day - 1) * 130 + 1
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = [
        unit(1, 'station', 10, 24, health=station_health),
        unit(2, 'worker', 9, 24), unit(3, 'worker', 12, 24),
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
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30),
        ('StationUpgradeVoucher1', 100), ('StationUpgradeVoucher2', 150),
        ('WallFixer', 10)]]
    return p


class RingFirstTests(unittest.TestCase):
    def test_both_workers_help_build_ring_until_complete(self):
        # 半圈未完成: 非采石工也参与采石建墙
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=75, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        workers = sorted(planner.turn.workers(), key=lambda r: r.unit_id)
        # 两人都被允许去采石(second worker 也 stone_fetcher)
        self.assertTrue(all(planner.economic.family(w) == 'stone' for w in workers))
        fetched = []
        for w in workers:
            routes = planner.route(w)
            fetched.append(planner.build_wall(w, routes))
        self.assertTrue(any(fetched), '半圈未完成时工人应去采石/建墙')

    def test_after_ring_complete_both_mine_ore(self):
        sites = wall_sites(Turn.load(payload()))
        if not sites:
            self.skipTest('no wall sites')
        p = payload(day=2, gold=75, walls=[unit(40 + i, 'wall', q.x, q.y, health=1000)
                                           for i, q in enumerate(sites)])
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertTrue(all(k == ('iron', 'copper') for k in kinds), kinds)


class BatchTests(unittest.TestCase):
    def test_mining_batch_is_large(self):
        p = payload(day=2)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        role = planner.turn.workers()[0]
        cands = planner.economic.mining_candidates(role, planner.route(role))
        self.assertTrue(cands)
        self.assertGreaterEqual(max(c['left'] for c in cands), 8,
                                '时间和背包充足时应允许大批量采集')

    def test_does_not_run_to_vendor_for_a_few_ores(self):
        # 只揣着 3 块矿且不在小贩旁 -> 不专程跑小贩（继续采）
        p = payload(day=2)
        p['teamOur']['roles'][1]['backpack'] = ['copper'] * 3
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        role = [w for w in planner.turn.workers() if w.unit_id == 2][0]
        planner.economic.act(role, planner.route(role))
        cmd = planner.commands.get('2', {})
        self.assertNotEqual(cmd.get('action'), 'sell', cmd)

    def test_buys_multiple_vouchers_in_one_trip(self):
        # 武器已满级 -> 围墙券批量买; 当天围墙配额 4, 金币足够 -> 一次 buy 多张
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=400, walls=[unit(40 + i, 'wall', q.x, q.y, health=1000)
                                            for i, q in enumerate(sites[:2])])
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('rocket', 'railgun'):
                u['level'] = 3
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        job = next((j for j in m.jobs.values() if j.get('item', '').startswith('Wall')), None)
        self.assertIsNotNone(job, m.jobs)
        self.assertGreaterEqual(job.get('num', 1), 2, job)

    def test_reserve_buys_multiple_fixers(self):
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=2, gold=200, walls=[unit(40 + i, 'wall', q.x, q.y, health=2000, level=3)
                                            for i, q in enumerate(sites[:2])])
        for u in p['teamOur']['roles']:
            if u['roleType'] in ('rocket', 'railgun', 'station'):
                u['level'] = 3
        m = Memory(day=2, day_wall_upgrades=4, day_weapon_upgrades=2)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        job = next((j for j in m.jobs.values() if j.get('item') == 'WallFixer'), None)
        self.assertIsNotNone(job, m.jobs)
        self.assertGreaterEqual(job.get('num', 1), 2, job)


class StationFallbackTests(unittest.TestCase):
    def test_day4_low_hp_station_is_top_priority(self):
        p = payload(day=STATION_FALLBACK_DAY, gold=200, station_health=STATION_FALLBACK_HP - 100)
        m = Memory(day=STATION_FALLBACK_DAY)
        planner = Planner(Turn.load(p), p, m)
        options = planner.economic.options()
        self.assertEqual(options[0][3], 'StationUpgradeVoucher1', [o[3] for o in options])

    def test_before_day4_low_hp_station_is_not_special(self):
        p = payload(day=3, gold=200, station_health=STATION_FALLBACK_HP - 100)
        m = Memory(day=3)
        planner = Planner(Turn.load(p), p, m)
        items = [o[3] for o in planner.economic.options()]
        self.assertIn('StationUpgradeVoucher1', items)
        # 第3天仍以围墙/武器配额为先, 基地券不在最前
        self.assertNotEqual(items[0], 'StationUpgradeVoucher1', items)

    def test_healthy_station_no_fallback_on_day4(self):
        p = payload(day=4, gold=200, station_health=1500)
        m = Memory(day=4)
        planner = Planner(Turn.load(p), p, m)
        first = planner.economic.options()[0][3]
        self.assertNotEqual(first, 'StationUpgradeVoucher1')


class TaskReuseTests(unittest.TestCase):
    def task_payload(self, desc):
        p = fixture()
        p['teamOur']['roles'] = [p['teamOur']['roles'][0], unit(4, 'pioneer', 12, 25)]
        p['mapInfo']['zones'] = [{'pos': {'x': 13, 'y': 26}, 'neutralType': 'challengerTaskPoint1'}]
        p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 13, 'y': 26}, 'isValid': True,
            'coldDownRounds': 0, 'timeoutRounds': 100, 'scoreReward': 50, 'goldReward': 30,
            'taskType': '自进化类1'}]
        p['phaseTask'] = desc
        return p

    def test_legacy_raw_command_is_not_replayed(self):
        m = Memory()
        m.skills.append({'task':'old task', 'command':'echo stale', 'answer':'old answer'})
        p = self.task_payload('old task')
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).run()
        self.assertNotEqual(planner.execute_cmd, 'echo stale')
        self.assertNotIn('4', planner.commands)

    def test_same_description_does_not_submit_saved_answer(self):
        m = Memory()
        m.skills.append({'task':'read current value', 'command':'cat value', 'answer':'stale'})
        p = self.task_payload('read current value')
        Tasks(Planner(Turn.load(p), p, m)).run()
        p['roundNo'] = 2
        p['lastCmdResult'] = 'fresh value: changed'
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).run()
        self.assertNotIn('4', planner.commands)
        self.assertIn('fresh value: changed', planner.prompt)

    def test_unknown_skill_is_guidance_for_current_context(self):
        from agent import templates
        m = Memory()
        desc, context = 'unknown graph problem', 'current graph'
        sig = templates.signature(desc, context).dump()
        m.skills.append({'family':'unknown', 'signature':sig, 'method':'use breadth-first search'})
        p = self.task_payload(desc)
        Tasks(Planner(Turn.load(p), p, m)).run()
        p['roundNo'] = 2
        p['lastCmdResult'] = context
        planner = Planner(Turn.load(p), p, m)
        Tasks(planner).run()
        self.assertIn('use breadth-first search', planner.prompt)
        self.assertIn(context, planner.prompt)

    def test_changed_unknown_context_does_not_match_skill(self):
        from agent import templates
        m = Memory()
        m.skills.append({'signature':templates.signature('unknown', 'old').dump(), 'method':'old method'})
        p = self.task_payload('unknown')
        tasks = Tasks(Planner(Turn.load(p), p, m))
        self.assertIsNone(tasks.reusable('unknown', 'new'))


if __name__ == '__main__':
    unittest.main()


class NightVoucherDeliveryTests(unittest.TestCase):
    """夜间必须先把背包里的升级券用掉, 再回武器塔旁防御。"""

    def night_payload(self, gold=0, wall_health=1000, worker_pos=None, backpack=None):
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=1, gold=gold,
                    walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=wall_health)])
        p['roundNo'] = 85                      # 夜晚
        p['teamOur']['roles'][1]['pos'] = worker_pos or {'x': 9, 'y': 22}
        if backpack:
            p['teamOur']['roles'][1]['backpack'] = list(backpack)
        return p, sites[0]

    def inner_cell(self, wall, station=Pos(10, 24)):
        return min((Pos(wall.x + dx, wall.y + dy)
                    for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
                   key=lambda q: distance(q, station))

    def test_night_uses_voucher_when_already_on_inner_side(self):
        # 已在炮位、贴墙且没有攻击目标 -> 原地使用券
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = self.inner_cell(sites[0]).dump()
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        stand = self.inner_cell(sites[0])
        next(u for u in p['teamOur']['roles'] if u['id']==10)['pos'] = {'x':stand.x,'y':stand.y-1}
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertIsNotNone(cmd, planner.commands)
        self.assertEqual(cmd['action'], 'use')
        self.assertEqual(cmd['name'], 'WallUpgradeVoucher1')

    def test_night_moves_to_inner_side_when_adjacent_outside(self):
        # 贴墙但在外侧 -> 先绕到内侧再用(外侧会被机器人打)
        sites = wall_sites(Turn.load(payload()))
        wall = sites[0]
        station = Pos(10, 24)
        outside = max((Pos(wall.x + dx, wall.y + dy)
                       for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy),
                      key=lambda q: distance(q, station))
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', wall.x, wall.y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = outside.dump()
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertEqual(cmd['action'], 'move', cmd)
        tgt = Pos.load(cmd['targetPos'][0])
        self.assertLess(distance(tgt, station), distance(outside, station), '应走向更靠基地的一侧')

    def test_night_returns_to_tower_instead_of_delivering_wall_voucher(self):
        # 夜间先保证炮位，不为墙券离开防御位置。
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=1, gold=0, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = {'x': 9, 'y': 21}     # 距墙若干格
        p['teamOur']['roles'][1]['backpack'] = ['WallUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertIsNotNone(cmd, planner.commands)
        self.assertEqual(cmd['action'], 'move', cmd)
        # 朝已分配的炮位移动，而不是配送围墙券。
        tgt = Pos.load(cmd['targetPos'][0])
        tower = next(t for t in planner.turn.weapons() if t.unit_id==m.tower_assignments[2])
        self.assertLess(distance(tgt, tower.pos), distance(Pos(9, 21), tower.pos))

    def test_night_weapon_voucher_also_delivered(self):
        p = payload(day=1, gold=0)
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = {'x': 10, 'y': 24}    # 基地旁
        p['teamOur']['roles'][1]['backpack'] = ['WeaponUpgradeVoucher1']
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertIsNotNone(cmd, planner.commands)
        self.assertIn(cmd['action'], ('move', 'use'))

    def test_no_buying_at_night(self):
        # 夜里不跑商店采购(采购是白天的事), 避免夜间离岗
        sites = wall_sites(Turn.load(payload()))
        p = payload(day=1, gold=200, walls=[unit(40, 'wall', sites[0].x, sites[0].y, health=1000)])
        p['roundNo'] = 85
        p['teamOur']['roles'][1]['pos'] = {'x': 9, 'y': 22}
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.run()
        cmd = planner.commands.get('2')
        self.assertNotEqual((cmd or {}).get('action'), 'buy', cmd)
