"""三项修改的回归测试。

1. 任务探测: 一次上下文采集后，未知任务进入通用 LLM 回退
2. 矿工分工: 一名工人采石、其余采矿，不出现两人都采石
3. 升级顺序: 第1天武器升级优先；第2天围墙升级优先，围墙升完再武器
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from test_strategy import fixture, unit
from agent.brain import Planner, decide_response, wall_sites
from agent.memory import Memory
from agent.protocol import Turn, Pos, distance
from agent import templates


def task_payload():
    p = fixture()
    p['teamOur']['roles'] = [p['teamOur']['roles'][0], unit(4, 'pioneer', 12, 25)]
    p['mapInfo']['zones'] = [{'pos': {'x': 13, 'y': 26}, 'neutralType': 'challengerTaskPoint1'}]
    p['teamOur']['playerTasks'] = [{'taskPosition': {'x': 13, 'y': 26}, 'isValid': True,
        'coldDownRounds': 0, 'timeoutRounds': 100, 'scoreReward': 50, 'goldReward': 30,
        'taskType': '自进化类1'}]
    return p


def two_worker_payload(day=1, gold=200, zones=None):
    p = fixture()
    p['roundNo'] = (day - 1) * 130 + 1
    p['teamOur']['goldNum'] = gold
    p['teamOur']['roles'] = [
        unit(1, 'station', 10, 24),
        unit(2, 'worker', 9, 24, capacity=100),
        unit(3, 'worker', 12, 24, capacity=100),
        unit(10, 'rocket', 9, 23), unit(11, 'rocket', 10, 23), unit(12, 'railgun', 11, 25),
    ]
    p['mapInfo']['zones'] = zones if zones is not None else [
        {'pos': {'x': 6, 'y': 24}, 'neutralType': 'stone'},
        {'pos': {'x': 6, 'y': 22}, 'neutralType': 'copper'},
        {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        {'pos': {'x': 7, 'y': 24}, 'neutralType': 'weaponShop'},
    ]
    p['weaponShopList'] = [{'name': n, 'price': v} for n, v in [
        ('WeaponUpgradeVoucher1', 100), ('WeaponUpgradeVoucher2', 150),
        ('WallUpgradeVoucher1', 20), ('WallUpgradeVoucher2', 30), ('WallFixer', 10)]]
    return p


class ProbeTests(unittest.TestCase):
    def test_one_context_command_then_generic_fallback(self):
        p = task_payload()
        p['phaseTask'] = '【自进化任务】调用未知接口查询文物编号'
        m = Memory()
        first = decide_response(p, m)
        self.assertEqual(first['executeCmd'], templates.locate_command(p['phaseTask']))
        self.assertFalse(first['prompt'])
        p['roundNo'] = 2
        p['lastCmdResult'] = '[exitCode:0]\nUNIQUE_CONTEXT_MARKER'
        second = decide_response(p, m)
        self.assertFalse(second['executeCmd'])
        self.assertIn('UNIQUE_CONTEXT_MARKER', second['prompt'])
        self.assertIn(p['phaseTask'], second['prompt'])
        self.assertEqual(m.llm_used, 0)

    def test_context_failure_goes_to_llm_without_probe_loop(self):
        p = task_payload()
        p['phaseTask'] = '请阅读 task_missing.md'
        m = Memory()
        decide_response(p, m)
        p['roundNo'] = 2
        p['lastCmdResult'] = '__TASK_ERROR "FileNotFoundError"'
        response = decide_response(p, m)
        self.assertTrue(response['prompt'])
        self.assertFalse(response['executeCmd'])

    def test_latest_command_result_is_in_fallback_prompt(self):
        p = task_payload()
        p['phaseTask'] = '未知任务'
        m = Memory()
        decide_response(p, m)
        p['roundNo'] = 2
        p['lastCmdResult'] = 'fresh context'
        response = decide_response(p, m)
        self.assertIn('fresh context', response['prompt'])
        self.assertIn('latest_result', response['prompt'])


class MiningRoleTests(unittest.TestCase):
    def test_day1_first_30_rounds_all_mine_ore_for_money(self):
        # 要求: 第1天一开始两名工人都去采铁/铜赚钱
        p = two_worker_payload(day=1)
        p['roundNo'] = 10                     # 第1天前 30 回合内
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertTrue(all(k == ('iron', 'copper') for k in kinds), kinds)

    def test_no_stone_fetching_in_first_30_rounds_of_day1(self):
        # 要求3: 第1天前30回合不采石(先赚钱), build_wall 也不得驱动采石
        p = two_worker_payload(day=1)
        p['roundNo'] = 10
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        for w in planner.turn.workers():
            self.assertFalse(planner.build_wall(w, planner.route(w)),
                             f'worker {w.unit_id} 不应在前30回合采石/建墙')

    def test_day1_after_30_rounds_both_fetch_stone_for_front_walls(self):
        # 要求: 第1天30回合后两人一起去采石, 把前方围墙修好
        p = two_worker_payload(day=1)
        p['roundNo'] = 40                     # 第1天第40回合
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertTrue(all(k == ('stone',) for k in kinds), kinds)

    def test_day2_both_fetch_stone_until_ring_complete(self):
        # 要求5: 第2天入夜前必须把半圈修完整 -> 未完成时两人一起采石
        p = two_worker_payload(day=2)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertTrue(all(k == ('stone',) for k in kinds), kinds)

    def test_ring_complete_then_all_mine_ore(self):
        p = two_worker_payload(day=2, gold=75)
        sites = wall_sites(Turn.load(p))
        p['teamOur']['roles'] += [unit(40 + i, 'wall', q.x, q.y, health=1000)
                                  for i, q in enumerate(sites)]
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        kinds = [planner.economic.wanted_kinds(w) for w in planner.turn.workers()]
        self.assertTrue(all(k == ('iron', 'copper') for k in kinds), kinds)

    def test_two_workers_pick_different_mines(self):
        # 要求6: 两名工人联合选点, 不同时采同一个矿
        p = two_worker_payload(day=2)
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        workers = sorted(planner.turn.workers(), key=lambda r: r.unit_id)
        chosen = []
        for w in workers:
            routes = planner.route(w)
            cands = planner.economic.mining_candidates(w, routes)
            self.assertTrue(cands, f'worker {w.unit_id} 应有候选矿点')
            best = max(cands, key=lambda c: c['score'])
            chosen.append(best['target'])
            planner.economic.mine_claims[best['target']] += best['left']
        self.assertNotEqual(chosen[0], chosen[1], '两名工人应选不同矿点')

    def test_nearer_home_mine_scores_higher(self):
        # 距离优先: 同等矿价下, 离家(回防线)更近的那处评分更高
        p = two_worker_payload(day=2, zones=[
            {'pos': {'x': 8, 'y': 24}, 'neutralType': 'iron'},      # 离家近
            {'pos': {'x': 30, 'y': 10}, 'neutralType': 'iron'},     # 离家远
            {'pos': {'x': 5, 'y': 24}, 'neutralType': 'vendor'},
        ])
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        role = planner.turn.workers()[0]
        cands = planner.economic.mining_candidates(role, planner.route(role))
        near = max((c for c in cands if c['target'] == Pos(8, 24)), key=lambda c: c['score'], default=None)
        far = max((c for c in cands if c['target'] == Pos(30, 10)), key=lambda c: c['score'], default=None)
        self.assertIsNotNone(near); self.assertIsNotNone(far)
        self.assertGreater(near['score'], far['score'])


class UpgradeOrderTests(unittest.TestCase):
    def planned(self, day):
        p = two_worker_payload(day=day, gold=400)
        p['teamOur']['roles'] += [unit(30, 'wall', 13, 24, health=1000)]
        m = Memory(day=day)
        planner = Planner(Turn.load(p), p, m)
        return planner, planner.economic.options()

    def test_day1_prefers_weapon_upgrades(self):
        planner, options = self.planned(day=1)
        first = options[0]
        self.assertTrue(first[3].startswith('Weapon'), [o[3] for o in options])
        weapon_pos = next(i for i, o in enumerate(options) if o[3].startswith('Weapon'))
        wall_pos = next(i for i, o in enumerate(options) if o[3].startswith('Wall'))
        self.assertLess(weapon_pos, wall_pos)

    def test_weapons_outrank_walls_until_maxed(self):
        # 规则(issue #4 总结): 武器升级优先级最高, 尽快升满; 围墙升级吃闲钱
        planner, options = self.planned(day=2)
        self.assertTrue([o for o in options if o[3].startswith('Wall')], '存在待升级围墙')
        weapon_pos = next(i for i, o in enumerate(options) if o[3].startswith('Weapon'))
        wall_pos = next(i for i, o in enumerate(options) if o[3].startswith('Wall'))
        self.assertLess(weapon_pos, wall_pos, [o[3] for o in options])

    def test_day3_weapons_still_first(self):
        planner, options = self.planned(day=3)
        wall_pos = next(i for i, o in enumerate(options) if o[3].startswith('Wall'))
        weapon_pos = next(i for i, o in enumerate(options) if o[3].startswith('Weapon'))
        self.assertLess(weapon_pos, wall_pos, [o[3] for o in options])

    def test_day2_weapons_resume_after_wall_phase_done(self):
        # 主要围墙已升到2级后 -> 武器升级接管
        p = two_worker_payload(day=3, gold=400)
        sites = wall_sites(Turn.load(p))
        p['teamOur']['roles'] += [unit(40 + i, 'wall', q.x, q.y, health=1000,
                                       level=(2 if i < len(sites) - 1 else 1))
                                  for i, q in enumerate(sites)]
        m = Memory(day=2)
        planner = Planner(Turn.load(p), p, m)
        options = planner.economic.options()
        weapon_pos = next(i for i, o in enumerate(options) if o[3].startswith('Weapon'))
        wall_pos = next(i for i, o in enumerate(options) if o[3].startswith('Wall'))
        self.assertLess(weapon_pos, wall_pos, [o[3] for o in options])

    def test_day1_weapon_voucher_not_blocked_by_missing_walls(self):
        # 第一天缺墙时，武器升级券仍可采购（此前会被 priority>=1 的守卫全部拦掉）
        p = two_worker_payload(day=1, gold=200)
        p['teamOur']['roles'] = [u for u in p['teamOur']['roles']
                                 if u['roleType'] in ('station', 'worker', 'rocket', 'railgun')]
        m = Memory(day=1)
        planner = Planner(Turn.load(p), p, m)
        planner.economic.prepare()
        bought = [j for j in m.jobs.values() if j.get('type') == 'upgrade']
        self.assertTrue(bought, m.jobs)
        self.assertTrue(bought[0]['item'].startswith('Weapon'), bought[0])

    def test_damaged_wall_repair_keeps_top_priority_on_both_days(self):
        for day in (1, 2):
            p = two_worker_payload(day=day, gold=400)
            p['teamOur']['roles'] += [unit(30, 'wall', 13, 24, health=200, level=3)]
            m = Memory(day=day)
            planner = Planner(Turn.load(p), p, m)
            options = planner.economic.options()
            # 三级墙已满级，保命手段是 WallFixer（修复包），应排在首位
            self.assertEqual(options[0][3], 'WallFixer', [o[3] for o in options])


if __name__ == '__main__':
    unittest.main()
