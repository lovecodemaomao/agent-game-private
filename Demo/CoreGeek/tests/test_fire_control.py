import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'),
               str(Path(__file__).resolve().parent)]
from test_strategy import fixture, unit
from agent.brain import Planner, decide_response
from agent.memory import Memory
from agent.protocol import Turn, Pos, distance
from agent.fire_control import our_target, plan_fire, raw_damage, select_targets


def battle():
    p = fixture(); p['roundNo'] = 85; p['teamOur']['goldNum'] = 0
    p['teamOur']['roles'] = [unit(1, 'station', 5, 15)]
    p['teamEnemy']['roles'] = [unit(200, 'station', 34, 15)]
    return p


def robot(uid, x, y, hp=20, team='challenger'):
    return unit(uid, 'smallRobot', x, y, health=hp, targetTeam=team)


class FireControlTests(unittest.TestCase):
    def test_explicit_team_beats_location_even_for_flankers(self):
        p = battle()
        p['robot']['roles'] = [robot(20, 30, 3), robot(21, 7, 15, team='defender')]
        turn = Turn.load(p)
        self.assertTrue(our_target(turn, turn.robots[0]))
        self.assertFalse(our_target(turn, turn.robots[1]))

    def test_missing_team_uses_bases_and_mirrors_for_right_side(self):
        p = battle(); p['robot']['roles'] = [robot(20, 12, 15, team=''), robot(21, 28, 15, team='')]
        turn = Turn.load(p)
        self.assertEqual([our_target(turn, r) for r in turn.robots], [True, False])
        p['teamOur']['roles'][0]['pos']['x'] = 34
        p['teamEnemy']['roles'][0]['pos']['x'] = 5
        turn = Turn.load(p)
        self.assertEqual([our_target(turn, r) for r in turn.robots], [False, True])

    def test_global_rocket_does_not_attack_opponents_wave(self):
        p = battle(); p['teamOur']['roles'].append(unit(10, 'rocket', 7, 15, level=3))
        p['robot']['roles'] = [robot(20, 12, 15, team='defender')]
        turn = Turn.load(p)
        self.assertEqual(plan_fire(turn, turn.weapons())[0], {})

    def test_pending_kill_still_blocks_gatling(self):
        p = battle(); p['teamOur']['roles'].append(unit(10, 'gatling', 7, 15, attackRange=20))
        p['robot']['roles'] = [robot(20, 10, 15, hp=10), robot(21, 13, 15, hp=20)]
        turn = Turn.load(p); tower = turn.weapons()[0]
        self.assertEqual(raw_damage(turn, tower, Pos(13, 15)), {20: 10})
        remaining = {20: 0, 21: 20}
        select_targets(turn, tower, remaining)
        self.assertEqual(remaining[21], 20)

    def test_railgun_energy_uses_actual_not_predicted_hp(self):
        p = battle(); p['teamOur']['roles'].append(unit(10, 'railgun', 7, 15, level=3, attackRange=20))
        p['robot']['roles'] = [robot(20, 10, 15, hp=20), robot(21, 13, 15, hp=40)]
        turn = Turn.load(p)
        self.assertEqual(raw_damage(turn, turn.weapons()[0], Pos(13, 15)), {20: 20, 21: 10})
        remaining = {20: 0, 21: 40}
        select_targets(turn, turn.weapons()[0], remaining)
        self.assertEqual(remaining[21], 30)

    def test_restricted_range_tower_gets_target_global_tower_can_leave(self):
        p = battle()
        p['teamOur']['roles'] += [unit(10, 'rocket', 7, 15, level=3),
                                  unit(11, 'rocket', 8, 15, attackRange=3)]
        p['robot']['roles'] = [robot(20, 10, 15, hp=20), robot(21, 17, 15, hp=60)]
        turn = Turn.load(p); plan, residual = plan_fire(turn, turn.weapons())
        self.assertEqual(residual, {20: 0, 21: 0})
        self.assertEqual(len(plan[10]), 3)
        self.assertEqual(len(plan[11]), 1)
        for tower in turn.weapons():
            self.assertTrue(all(distance(tower.pos, pos) <= tower.range_of_attack() for pos in plan[tower.unit_id]))

    def test_fleet_avoids_duplicate_volleys_and_preserves_cooldown(self):
        p = battle()
        p['teamOur']['roles'] += [unit(10, 'rocket', 7, 15), unit(11, 'rocket', 7, 17)]
        p['robot']['roles'] = [robot(20, 10, 15, hp=20)]
        turn = Turn.load(p); plan, residual = plan_fire(turn, turn.weapons())
        self.assertEqual(residual[20], 0)
        self.assertEqual(len(plan), 1)

    def test_multi_missile_damage_counts_every_missile(self):
        p = battle(); p['teamOur']['roles'].append(unit(10, 'rocket', 7, 15, level=3))
        p['robot']['roles'] = [robot(20, 10, 15, hp=15), robot(21, 14, 15, hp=15)]
        turn = Turn.load(p); plan, residual = plan_fire(turn, turn.weapons())
        self.assertEqual(len(plan[10]), 3)
        actual = {r.robot_id: r.health for r in turn.robots}
        for point in plan[10]:
            for rid, damage in raw_damage(turn, turn.weapons()[0], point).items():
                actual[rid] = max(0, actual[rid] - damage)
        self.assertEqual(actual, residual)
        raw_total = sum(sum(raw_damage(turn, turn.weapons()[0], point).values()) for point in plan[10])
        self.assertEqual(raw_total, 40)  # 30 effective, 10 unavoidable overkill for this volley

    def test_gatling_cone_remains_legal(self):
        p = battle(); p['teamOur']['roles'].append(unit(10, 'gatling', 12, 15, level=3, attackRange=10))
        p['robot']['roles'] = [robot(20, 9, 15), robot(21, 15, 15), robot(22, 12, 18)]
        turn = Turn.load(p); plan, _ = plan_fire(turn, turn.weapons())
        points = plan[10]; self.assertEqual(len(points), 3)
        for a in points:
            for b in points:
                self.assertGreaterEqual((a.x-12)*(b.x-12)+(a.y-15)*(b.y-15), 0)

    def test_attack_assignment_is_deterministic(self):
        p = battle(); p['teamOur']['roles'].append(unit(10, 'rocket', 7, 15, level=3))
        p['robot']['roles'] = [robot(20, 10, 15), robot(21, 14, 15)]
        turn = Turn.load(p); expected = plan_fire(turn, turn.weapons())
        p['robot']['roles'].reverse(); turn = Turn.load(p)
        self.assertEqual(plan_fire(turn, turn.weapons()), expected)


class OperatorsTests(unittest.TestCase):
    def test_three_operators_fire_even_with_pending_deliveries(self):
        p = battle()
        for i, y in enumerate((12, 15, 18)):
            p['teamOur']['roles'] += [unit(2+i, 'pioneer' if i==2 else 'worker', 7, y,
                                          backpack=['WallUpgradeVoucher1']),
                                      unit(10+i, 'rocket', 8, y)]
        p['teamOur']['roles'].append(unit(40, 'wall', 12, 15, health=400))
        p['robot']['roles'] = [robot(20, 13, 15, hp=500)]
        result = decide_response(p, Memory())['roleCommandMap']
        attacks = [c for c in result.values() if c['action'] == 'attack']
        self.assertEqual(len(attacks), 3)
        self.assertEqual({c['controllerId'] for c in attacks}, {'2','3','4'})
        self.assertFalse(any(c['action'] in ('move','use') for c in result.values()))

    def test_idle_controllers_remain_at_posts(self):
        p = battle()
        p['teamOur']['roles'] += [unit(2, 'worker', 7, 15), unit(10, 'rocket', 8, 15, cooldown=2)]
        m = Memory(); self.assertEqual(decide_response(p, m)['roleCommandMap'], {})
        self.assertEqual(m.tower_assignments[2], 10)

    def test_idle_operators_do_not_double_repair_one_wall(self):
        p = battle()
        p['teamOur']['roles'] += [unit(2,'worker',7,15,backpack=['WallFixer']),
                                  unit(3,'worker',7,17,backpack=['WallFixer']),
                                  unit(10,'rocket',8,14,cooldown=2),
                                  unit(11,'rocket',8,18,cooldown=2),
                                  unit(40,'wall',8,16,health=30)]
        p['robot']['roles'] = [unit(90,'bossRobot',9,16,health=800,targetTeam='challenger')]
        result = decide_response(p, Memory())['roleCommandMap']
        self.assertEqual(sum(c['action']=='use' for c in result.values()), 1)
        self.assertFalse(any(c.get('controllerId')==uid for uid,c in result.items() if c['action']=='use'))


if __name__ == '__main__':
    unittest.main()
