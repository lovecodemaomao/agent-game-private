import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent.brain import decide, ring, wall_sites, select_targets, Planner
from agent.grid import Routes, next_step
from agent.protocol import Turn, Pos, distance


def unit(uid, kind, x, y, **kw):
    return {'id': uid, 'roleType': kind, 'pos': {'x': x, 'y': y},
            'health': 1500 if kind == 'station' else 220,
            'level': 1, 'backpack': [], 'backPackCapability': 100, **kw}


def fixture():
    return {'roundNo': 1, 'mapInfo': {'width': 41, 'height': 32, 'zones': []},
            'teamOur': {'type': 'challenger', 'goldNum': 75, 'roles': [
                unit(1, 'station', 10, 24), unit(2, 'worker', 9, 24),
                unit(3, 'worker', 12, 24), unit(4, 'pioneer', 10, 25)]},
            'teamEnemy': {'roles': []}, 'robot': {'roles': []},
            'vendorShopList': [{'name': k, 'price': v} for k, v in [('stone',1),('iron',3),('copper',5)]],
            'weaponShopList': [{'name': 'WeaponUpgradeVoucher1', 'price': 100}]}


def apply_construction(p, commands):
    actors = {str(r['id']): r for r in p['teamOur']['roles']}
    targets = set()
    for uid, cmd in commands.items():
        actor = actors[uid]
        if cmd['action'] == 'build':
            pos = Pos.load(cmd['targetPos'][0])
            assert distance(Pos.load(actor['pos']), pos) == 1
            assert pos not in targets
            targets.add(pos)
            if cmd['name'] == 'wall':
                actor['backpack'].remove('stone')
            else:
                p['teamOur']['goldNum'] -= 25
                assert p['teamOur']['goldNum'] >= 0
            p['teamOur']['roles'].append(unit(100+len(p['teamOur']['roles']), cmd['name'], pos.x, pos.y, health=1000))
        elif cmd['action'] == 'move':
            actor['pos'] = cmd['targetPos'][0]
    p['roundNo'] += 1


class BasicStrategyTests(unittest.TestCase):
    def test_spawn_builds_two_then_completes_loadout(self):
        # Shared operator layout may require initial movement; complete the legal fleet.
        p = fixture()
        for _ in range(8):
            apply_construction(p, decide(p))
        kinds = [r['roleType'] for r in p['teamOur']['roles']]
        self.assertEqual(kinds.count('rocket'), 2, kinds)
        self.assertEqual(kinds.count('railgun'), 1)
        self.assertEqual(p['teamOur']['goldNum'], 0)
        turn = Turn.load(p)
        rockets = [t for t in turn.weapons() if t.kind=='rocket']
        from agent.grid import neighbours
        shared = set(neighbours(rockets[0].pos)) & set(neighbours(rockets[1].pos))
        self.assertTrue(shared - {t.pos for t in turn.weapons()} - set(__import__("agent.protocol",fromlist=["station_footprint"]).station_footprint(turn.station().pos)))

    def test_shared_budget(self):
        p = fixture(); p['teamOur']['goldNum'] = 25
        self.assertEqual(sum(c['action']=='build' for c in decide(p).values()), 1)

    def test_existing_three_towers_never_build_fourth(self):
        p = fixture()
        p['teamOur']['roles'] += [unit(10,'rocket',9,22), unit(11,'rocket',11,25), unit(12,'railgun',12,22)]
        self.assertFalse(any(c['action']=='build' and c['name']!='wall' for c in decide(p).values()))

    def test_build_rings_and_mirrored_forward_walls(self):
        for x in (10, 29):
            p = fixture(); p['teamOur']['roles'][0]['pos']['x'] = x
            turn = Turn.load(p)
            self.assertEqual(len(ring(turn,1)),12)
            self.assertEqual(len(ring(turn,2)),20)
            self.assertEqual(len(wall_sites(turn)),4)
            sign = 1 if x < 20 else -1
            self.assertTrue(all(sign*(q.x-(x+0.5))>0 for q in wall_sites(turn)))

    def test_enemy_footprint_blocks_paths(self):
        p = fixture(); p['teamEnemy']['roles'] = [unit(50,'station',7,24)]
        turn = Turn.load(p); paths = Routes(turn,turn.workers()[0])
        self.assertNotIn(Pos(8,23), paths.cost)
        self.assertNotIn(Pos(7,24), paths.cost)

    def test_path_to_current_position_is_noop(self):
        turn = Turn.load(fixture()); role = turn.workers()[0]
        self.assertIsNone(next_step(turn,role,role.pos))

    def test_dusk_returns_instead_of_mining(self):
        p = fixture(); p['roundNo']=68
        p['teamOur']['roles'] += [unit(10,'rocket',9,22)]
        p['teamOur']['roles'][1]['pos']={'x':3,'y':15}
        p['mapInfo']['zones']=[{'pos':{'x':3,'y':14},'neutralType':'copper'}]
        self.assertNotIn('collect', [c['action'] for c in decide(p).values()])

    def test_aoe_uses_empty_center(self):
        p=fixture(); p['teamOur']['roles'] += [unit(10,'rocket',12,23)]
        p['robot']['roles']=[unit(20,'smallRobot',16,22,health=40),unit(21,'smallRobot',18,22,health=40),unit(22,'smallRobot',16,24,health=40),unit(23,'smallRobot',18,24,health=40)]
        turn=Turn.load(p); remaining={r.robot_id:r.health for r in turn.robots}
        selected=select_targets(turn,turn.weapons()[0],remaining)
        self.assertEqual(len(selected),1)
        self.assertTrue(all(distance(selected[0],r.pos)<=1 for r in turn.robots))
        self.assertEqual(selected[0],Pos(17,23))
        self.assertEqual(sum(remaining.values()),120)

    def test_upgraded_rocket_repeats_and_matches_level(self):
        p=fixture(); p['teamOur']['roles'] += [unit(10,'rocket',12,23,level=3)]
        p['robot']['roles']=[unit(20,'largeRobot',15,23,health=500)]
        turn=Turn.load(p); remaining={20:500}
        self.assertEqual(select_targets(turn,turn.weapons()[0],remaining),[Pos(15,23)]*3)
        self.assertEqual(remaining[20],440)

    def test_cooldown_and_unique_controllers(self):
        p=fixture(); p['roundNo']=71
        p['teamOur']['roles'] += [unit(10,'rocket',9,23,cooldown=2),unit(11,'railgun',12,23)]
        p['robot']['roles']=[unit(20,'smallRobot',15,23,health=40)]
        commands=decide(p)
        self.assertNotIn('10',commands)
        attacks=[c for c in commands.values() if c['action']=='attack']
        self.assertTrue(attacks)
        self.assertEqual(len({c['controllerId'] for c in attacks}),len(attacks))
        self.assertTrue(all(c['controllerId'] not in commands for c in attacks))

    def test_wall_stone_batch_and_sell(self):
        p=fixture(); p['teamOur']['roles'][1]['backpack']=['copper']*10
        p['mapInfo']['zones']=[{'pos':{'x':8,'y':24},'neutralType':'vendor'}]
        planner=Planner(Turn.load(p),p)
        role=planner.turn.workers()[0]
        planner.economy(role,planner.route(role))
        self.assertEqual(planner.commands['2'],{'action':'sell','name':'copper','num':10})

    def test_upgrade_purchase_budget_and_delivery(self):
        p=fixture(); p['roundNo']=131; p['teamOur']['goldNum']=100
        p['teamOur']['roles'] += [unit(10,'rocket',9,23),unit(11,'rocket',12,23),unit(12,'railgun',11,25)]
        p['mapInfo']['zones']=[{'pos':{'x':8,'y':24},'neutralType':'weaponShop'}]
        planner=Planner(Turn.load(p),p); role=planner.turn.workers()[0]
        planner.economy(role,planner.route(role))
        self.assertEqual(planner.commands['2']['action'],'buy')
        self.assertEqual(planner.gold,0)
        p['teamOur']['roles'][1]['backpack']=['WeaponUpgradeVoucher1']
        planner=Planner(Turn.load(p),p); role=planner.turn.workers()[0]
        self.assertTrue(planner.deliver_upgrade(role,planner.route(role)))
        self.assertEqual(planner.commands['2']['action'],'use')

    def test_http_response_envelope(self):
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.request import Request, urlopen
        from agent.server import Handler
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            req=Request(f'http://127.0.0.1:{server.server_port}',
                        data=json.dumps(fixture()).encode(),
                        headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=5) as response:
                result=json.load(response)
            self.assertEqual(set(result),{'roleCommandMap','prompt','executeCmd'})
            self.assertGreaterEqual(sum(c['action']=='build' for c in result['roleCommandMap'].values()),1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_first_day_building_and_return_simulation(self):
        p=fixture()
        p['mapInfo']['zones']=[
            {'pos': {'x': 7, 'y': 23}, 'neutralType': 'stone'},
            {'pos': {'x': 7, 'y': 26}, 'neutralType': 'copper'},
            {'pos': {'x': 6, 'y': 25}, 'neutralType': 'vendor'},
            {'pos': {'x': 6, 'y': 24}, 'neutralType': 'weaponShop'}]
        # A deterministic day-only action harness, not a robot/judge simulator.
        for round_no in range(1,71):
            turn=Turn.load(p)
            commands=decide(p)
            actors={str(r['id']):r for r in p['teamOur']['roles']}
            next_positions=[]
            for uid,cmd in commands.items():
                actor=actors[uid]
                action=cmd['action']
                if action=='move':
                    target=Pos.load(cmd['targetPos'][0])
                    self.assertEqual(distance(Pos.load(actor['pos']),target),1)
                    self.assertNotIn(target,turn.occupied_cells())
                    self.assertNotIn(target,next_positions)
                    next_positions.append(target)
                elif action=='build':
                    target=Pos.load(cmd['targetPos'][0])
                    self.assertIn(target,ring(turn,2 if cmd['name']=='wall' else 1))
                elif action=='collect':
                    target=Pos.load(cmd['targetPos'][0])
                    self.assertEqual(distance(Pos.load(actor['pos']),target),1)
                    actor['backpack'].append(turn.zones[target])
                elif action=='sell':
                    for _ in range(cmd['num']):
                        actor['backpack'].remove(cmd['name'])
                    p['teamOur']['goldNum']+=cmd['num']*dict(stone=1,iron=3,copper=5)[cmd['name']]
                elif action=='buy':
                    p['teamOur']['goldNum']-=100
                    self.assertGreaterEqual(p['teamOur']['goldNum'],0)
                    actor['backpack'].append(cmd['name'])
                elif action=='use':
                    actor['backpack'].remove(cmd['name'])
                    target=next(r for r in p['teamOur']['roles'] if r['pos']==cmd['targetPos'][0])
                    target['level']+=1
            apply_construction(p,commands)
        turn=Turn.load(p)
        self.assertEqual(len(turn.weapons()),3)
        self.assertEqual(len(turn.walls()),4)
        # Day1 has an open rear; retain the existing day75 return deadline.
        # Simulate the final approach rather than infer seating from distance to a shop.
        for _ in range(5):
            commands=decide(p)
            for role in p['teamOur']['roles']:
                cmd=commands.get(str(role['id']))
                if cmd and cmd['action']=='move': role['pos']=cmd['targetPos'][0]
            p['roundNo']+=1
        turn=Turn.load(p)
        self.assertTrue(all(any(distance(r.pos,t.pos)<=1 for t in turn.weapons())
                            for r in turn.controllable()))

    def test_railgun_energy_conserved(self):
        p=fixture(); p['teamOur']['roles'] += [unit(10,'railgun',12,23,level=3)]
        p['robot']['roles']=[unit(20,'smallRobot',14,23,health=10),unit(21,'smallRobot',15,23,health=40)]
        turn=Turn.load(p); remaining={20:10,21:40}
        select_targets(turn,turn.weapons()[0],remaining)
        self.assertEqual(remaining,{20:0,21:20})


if __name__=='__main__':
    unittest.main()
