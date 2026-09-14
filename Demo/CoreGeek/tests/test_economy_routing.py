"""Full-level procurement, comparable journeys and changing route reservations."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_issue4_efficiency import payload
from test_strategy import unit
from agent.brain import Planner
from agent.grid import Routes
from agent.memory import Memory
from agent.protocol import Turn


class EconomyRoutingTests(unittest.TestCase):
    def test_full_level_fleet_can_restock_fixers_after_daily_reset(self):
        p = payload(day=2, gold=40, walls=[unit(40, 'wall', 13, 24, level=3, health=2000)])
        for role in p['teamOur']['roles']:
            if role['roleType'] in ('station', 'rocket', 'railgun'):
                role['level'] = 3
        memory = Memory(day=2)
        planner = Planner(Turn.load(p), p, memory)
        planner.economic.prepare()
        orders = [j for j in memory.jobs.values() if j.get('item') == 'WallFixer']
        self.assertTrue(orders, 'Unachievable weapon quota must not block repair reserves')
        self.assertGreaterEqual(orders[0]['num'], 2)

    def test_unfinished_fleet_still_reserves_upgrade_quota(self):
        p = payload(day=2, gold=40, walls=[unit(40, 'wall', 13, 24, level=3, health=2000)])
        planner = Planner(Turn.load(p), p, Memory(day=2))
        planner.economic.prepare()
        self.assertGreater(planner.economic.weapon_quota_left(), 0)
        self.assertFalse(any(j.get('item') == 'WallFixer' for j in planner.memory.jobs.values()))

    def test_sale_and_mine_scores_include_the_same_return_leg(self):
        p = payload(day=2)
        p['teamOur']['roles'][1]['backpack'] = ['copper'] * 6
        planner = Planner(Turn.load(p), p, Memory(day=2))
        worker = planner.turn.workers()[0]
        routes = planner.route(worker)
        sales = planner.economic.sale_candidates(worker, routes)
        mines = planner.economic.mining_candidates(worker, routes)
        self.assertTrue(sales and mines)
        for sale in sales:
            duration = routes.cost[sale['seat']] + len(sale['stock']) + planner.home_cost(worker, sale['seat'])
            self.assertAlmostEqual(sale['score'], sale['value'] / max(1, duration))
        for mine in mines:
            duration = (routes.cost[mine['entry']] + mine['left']
                        + planner.geo.field([mine['exit']])[mine['entry']]
                        + len({'copper', mine['kind']}) + planner.home_cost(worker, mine['exit']))
            value = 6 * planner.prices['copper'] + mine['left'] * mine['price']
            self.assertAlmostEqual(mine['score'], value / max(1, duration))

    def test_route_reuse_rebuilds_for_reservations_and_failed_steps(self):
        p = payload(day=2)
        planner = Planner(Turn.load(p), p, Memory(day=2))
        worker = planner.turn.workers()[0]
        with patch('agent.brain.Routes', wraps=Routes) as build:
            original = planner.route(worker)
            for _ in range(10):
                planner.route(worker)
            self.assertEqual(build.call_count, 1)
            reserved = next(q for q, cost in original.cost.items() if cost == 1)
            planner.reserved.add(reserved)
            updated = planner.route(worker)
            self.assertNotIn(reserved, updated.cost)
            blocked = next(q for q, cost in updated.cost.items() if cost == 1)
            planner.memory.failed_steps[worker.unit_id] = (blocked, planner.turn.round_no)
            failed = planner.route(worker)
            self.assertNotIn(blocked, failed.cost)
            self.assertEqual(build.call_count, 3)
            planner.memory.failed_steps.clear()
            self.assertIn(blocked, planner.route(worker).cost)


if __name__ == '__main__':
    unittest.main()
