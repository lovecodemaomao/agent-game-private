"""Deterministic night-only Demo benchmark, NOT the official judge.

python Demo/CoreGeek/tests/benchmark_combat_defense.py --source Demo/CoreGeek/src
Same initial resources/layout for either source; levels are capability fixtures,
not a claim that the economic policy earns those upgrades. Robot pathing is
straight approach + obstacle attack, cooldown=3; attacks settle at round end.
"""
import argparse
import importlib
import json
from pathlib import Path
import sys
import time
import subprocess
import hashlib

WAVES = [(30,4,0,0),(35,10,0,0),(40,15,3,0),(45,20,4,0),
         (50,20,4,1),(55,25,5,1),(57,27,5,2)]
LEVELS = [(2,2,1),(2,2,2),(3,2,2),(3,3,2),(3,3,3),(3,3,3),(3,3,3)]
TYPES = [('smallRobot',40,5),('middleRobot',60,10),('largeRobot',500,20),('bossRobot',800,40)]


def unit(uid,kind,x,y,health=1000,level=1,backpack=()):
    return dict(id=uid,roleType=kind,pos=dict(x=x,y=y),health=health,level=level,
                cooldown=0,backpack=list(backpack),backPackCapability=100)


def scenario(day, packs=3, mirror=False):
    a,b,c = LEVELS[day-1]
    roles = [unit(1,'station',5,15,1500), unit(2,'worker',7,14,220),
             unit(3,'worker',4,14,220),unit(4,'pioneer',7,16,200,backpack=['WallFixer']*packs),
             unit(10,'rocket',7,13,1000,a),unit(11,'rocket',7,15,1000,b),
             unit(12,'railgun',4,13,1000,c)]
    # Identical defensive fixture for old/new. Four front walls in day1,
    # half ring day2, complete L2 ring day3+, including a permanently L1 gate.
    ring = [(x,y) for x in range(3,9) for y in range(12,18)
            if x in (3,8) or y in (12,17)]
    sites = [(8,y) for y in range(13,17)] if day==1 else [q for q in ring if q[0]>=6] if day==2 else ring
    for i,(x,y) in enumerate(sites):
        level = 1 if day==1 or (x,y)==(3,14) else 2
        roles.append(unit(40+i,'wall',x,y,1000 if level==1 else 1500,level))
    robots = []
    for (kind,hp,power),count in reversed(list(zip(TYPES,WAVES[day-1]))):
        for _ in range(count):
            i = len(robots)
            r = unit(1000+i,kind,19+i//9,11+i%9,hp)
            r.update(targetTeam='challenger',power=power,robot_cooldown=0)
            robots.append(r)
    if mirror:
        for u in roles+robots:
            u['pos']['x'] = (39 if u['roleType']=='station' else 40)-u['pos']['x']
    return dict(roundNo=(day-1)*130+71,mapInfo=dict(width=41,height=32,zones=[]),
                teamOur=dict(type='challenger',goldNum=0,roles=roles),
                teamEnemy=dict(type='defender',roles=[]),robot=dict(roles=robots),
                vendorShopList=[],weaponShopList=[],worldNews={})


def run_case(day,packs,mirror,respond,api,ballistics):
    p = scenario(day,packs,mirror)
    metrics = dict(day=day,packs=packs,mirror=mirror,kills=0,remaining_robots_at_dawn=0,
                   clear_round=None,rocket_shots=0,railgun_shots=0,wall_damage=0,
                   walls_destroyed=0,station_damage=0,wall_fixer_used=0,
                   combat_mode_switch_round=None,max_decision_ms=0)
    metrics.update(effective_damage=0,overkill=0,station_hp=1500)
    for tick in range(60):
        turn = api.Turn.load(p)
        start = time.perf_counter(); response = respond(p)
        elapsed = (time.perf_counter()-start)*1000
        metrics['max_decision_ms'] = max(metrics['max_decision_ms'],elapsed)
        assert elapsed < 5000, ('decision timeout',day,tick,elapsed)
        commands = response['roleCommandMap']
        actors = {str(u['id']):u for u in p['teamOur']['roles'] if u['health']>0}
        initial_robots = {r.robot_id:r for r in turn.robots}
        predicted = {}; incoming = {}; moves = {}; used = set(); fired = set()
        p['lastRoundRoleActionResults'] = {}
        for uid,cmd in commands.items():
            assert uid in actors, ('unknown actor',uid)
            actor = actors[uid]; action = cmd['action']
            assert action not in ('remove','build'), ('night construction',cmd)
            if action == 'attack':
                controller = cmd['controllerId']
                assert controller in actors and controller not in used and controller not in commands
                assert actors[controller]['roleType'] in ('worker','pioneer')
                used.add(controller)
                p['lastRoundRoleActionResults'][uid] = True
                assert api.distance(api.Pos.load(actors[controller]['pos']),api.Pos.load(actor['pos'])) == 1
                tower = next(t for t in turn.weapons() if str(t.unit_id)==uid)
                assert tower.cooldown == 0
                points = [api.Pos.load(q) for q in cmd['targetPos']]
                assert len(points) == (tower.level if tower.kind in ('rocket','gatling') else 1)
                for point in points:
                    assert 0<=point.x<41 and 0<=point.y<32
                    assert api.distance(point,tower.pos)<=tower.range_of_attack()
                    for rid,damage in ballistics.raw_damage(turn,tower,point).items():
                        predicted[rid] = predicted.get(rid,0)+damage
                key = 'rocket_shots' if tower.kind=='rocket' else 'railgun_shots'
                metrics[key] += len(points)
                fired.add(uid)
            elif action == 'use':
                assert cmd['name']=='WallFixer', ('unexpected resource',cmd)
                assert 'WallFixer' in actor['backpack']
                pos = api.Pos.load(cmd['targetPos'][0])
                assert api.distance(api.Pos.load(actor['pos']),pos)==1
                target = next(u for u in actors.values() if api.Pos.load(u['pos'])==pos)
                assert target['roleType']=='wall'
                target['health'] = (1000,1500,2000)[target['level']-1]
                actor['backpack'].remove('WallFixer'); metrics['wall_fixer_used'] += 1
            elif action == 'move':
                q = api.Pos.load(cmd['targetPos'][0])
                assert api.distance(api.Pos.load(actor['pos']),q)==1 and turn.land(q)
                moves[uid] = q
            else:
                raise AssertionError(('unexpected night action',action))
        # Character movement: blocked/contested destinations fail without teleporting.
        for uid,q in moves.items():
            role = next(r for r in turn.controllable() if str(r.unit_id)==uid)
            success = q not in turn.blocked(role) and sum(q==x for x in moves.values())==1
            p['lastRoundRoleActionResults'][uid] = success
            if success:
                actors[uid]['pos'] = q.dump()
        occupied = {}
        for uid,u in actors.items():
            cells = api.station_footprint(api.Pos.load(u['pos'])) if u['roleType']=='station' else [api.Pos.load(u['pos'])]
            for q in cells: occupied[q] = uid
        robot_cells = {api.Pos.load(r['pos']) for r in p['robot']['roles']}
        station = api.Pos.load(actors['1']['pos'])
        for r in p['robot']['roles']:
            pos = api.Pos.load(r['pos'])
            # Retain lane until reaching the front; then approach the closest base cell.
            goal = min(api.station_footprint(station),key=lambda q:api.distance(pos,q))
            dx = (goal.x>pos.x)-(goal.x<pos.x)
            dy = 0 if abs(goal.x-pos.x)>3 else (goal.y>pos.y)-(goal.y<pos.y)
            nxt = api.Pos(pos.x+dx,pos.y+dy)
            if nxt in occupied:
                if r['robot_cooldown']==0:
                    target = occupied[nxt]
                    incoming[target] = incoming.get(target,0)+r['power']
                    r['robot_cooldown'] = 3
                else:
                    r['robot_cooldown'] -= 1
            else:
                r['robot_cooldown'] = max(0,r['robot_cooldown']-1)
                if nxt not in robot_cells:
                    robot_cells.remove(pos); robot_cells.add(nxt); r['pos'] = nxt.dump()
        # Simultaneous damage: even predicted-dead robots made their incoming attack.
        for r in p['robot']['roles']:
            damage=predicted.get(r['id'],0)
            metrics['effective_damage']+=min(r['health'],damage)
            metrics['overkill']+=max(0,damage-r['health'])
            r['health'] = max(0,r['health']-damage)
        metrics['kills'] += sum(r['health']==0 for r in p['robot']['roles'])
        p['robot']['roles'] = [r for r in p['robot']['roles'] if r['health']>0]
        for uid,damage in incoming.items():
            u=actors[uid]; actual=min(u['health'],damage); u['health']-=actual
            if u['roleType']=='wall':
                metrics['wall_damage']+=actual; metrics['walls_destroyed']+=int(u['health']==0)
            elif u['roleType']=='station': metrics['station_damage']+=actual
        for uid,u in actors.items():
            if u['roleType']=='rocket':
                u['cooldown'] = 3 if uid in fired else max(0,u['cooldown']-1)
        p['roundNo'] += 1
        if not p['robot']['roles']:
            metrics['clear_round']=71+tick; break
        if actors['1']['health']<=0:
            break
    metrics['remaining_robots_at_dawn']=len(p['robot']['roles'])
    metrics['station_alive']=next(u['health'] for u in p['teamOur']['roles'] if u['id']==1)>0
    metrics['station_hp']=next(u['health'] for u in p['teamOur']['roles'] if u['id']==1)
    metrics['max_decision_ms']=round(metrics['max_decision_ms'],2)
    return metrics


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default=str(Path(__file__).resolve().parents[1]/'src'))
    parser.add_argument('--output')
    parser.add_argument('--mirror',action='store_true')
    parser.add_argument('--compare',help='Baseline JSON; block survival regressions under identical fixtures')
    args=parser.parse_args()
    sys.path.insert(0,str(Path(args.source).resolve()))
    api=importlib.import_module('agent.protocol'); fc=importlib.import_module('agent.fire_control')
    Agent=importlib.import_module('agent.runtime').Agent
    results=[]
    for packs in (3,0):
        for day in range(1,8):
            agent=Agent()
            row=run_case(day,packs,args.mirror,agent.respond,api,fc)
            memory=next(iter(agent.sessions.values()))
            row['combat_mode_switch_round']=getattr(memory,'combat_switch_round',None)
            results.append(row)
            print(json.dumps(row),flush=True)
    sha=subprocess.check_output(['git','-C',str(Path(args.source).resolve()),'rev-parse','HEAD'],text=True).strip()
    source_root=Path(args.source).resolve()
    source_hash=hashlib.sha256(b''.join(p.relative_to(source_root).as_posix().encode()+p.read_bytes()
                              for p in sorted(source_root.rglob('*.py')))).hexdigest()
    report={'assumptions':__doc__,'source_sha':sha,'source_hash':source_hash,
            'waves':[list(w) for w in WAVES],'levels':[list(w) for w in LEVELS],'results':results}
    if args.output:
        Path(args.output).write_text(json.dumps(report,indent=2),encoding='utf-8')
    if args.compare:
        baseline=json.loads(Path(args.compare).read_text(encoding='utf-8'))
        assert baseline['waves']==report['waves'] and baseline['levels']==report['levels']
        before={(r['day'],r['packs'],r['mirror']):r for r in baseline['results']}
        for row in results:
            old=before[row['day'],row['packs'],row['mirror']]
            assert not old['station_alive'] or row['station_alive'], ('survival regression',row)


if __name__=='__main__': main()
