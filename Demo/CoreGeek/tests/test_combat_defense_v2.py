"""Behavioral regressions for the formal combat/defense policy."""
import copy
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from test_strategy import fixture, unit
from test_fire_control import battle, robot
from agent.brain import Planner, LOADOUT, decide_response, ring, wall_sites
from agent.memory import Memory
from agent.protocol import Turn, Pos, distance
from agent import fire_control as fc
import pytest


def arena(round_no=85, level=2):
    p = battle(); p['roundNo'] = round_no
    p['teamOur']['roles'] += [unit(2,'worker',7,14), unit(3,'worker',6,16),
        unit(4,'pioneer',7,16,backpack=['WallFixer']),
        unit(10,'rocket',7,13,level=level), unit(11,'rocket',7,15,level=level),
        unit(12,'railgun',5,16,level=level), unit(40,'wall',8,15,health=1000)]
    return p


def test_default_and_staged_walls():
    assert LOADOUT == ('rocket','rocket','railgun')
    for x in (5,34):
        p = battle(); p['teamOur']['roles'][0]['pos']['x'] = x
        for day, count in ((1,4),(2,10),(3,20)):
            p['roundNo'] = (day-1)*130+1
            assert len(wall_sites(Turn.load(p))) == count


def test_modes_latch_and_reset():
    p = arena(); p['robot']['roles'] = [robot(100,18,15,hp=800)]
    m = Memory(); t = Turn.load(p)
    assert m.combat_mode == fc.KILL_ALL_MODE
    assert m.choose_combat_mode(t, .5, False) == fc.KILL_ALL_MODE
    p['roundNo'] += 1
    assert m.choose_combat_mode(Turn.load(p), .5, False) == fc.SURVIVAL_MODE
    p['roundNo'] += 1
    assert m.choose_combat_mode(Turn.load(p), 10, False) == fc.SURVIVAL_MODE
    p['roundNo'] = 201
    assert m.choose_combat_mode(Turn.load(p), 10, False) == fc.KILL_ALL_MODE


def test_dawn_hold_and_immediate_risk():
    p = arena(129); wall = p['teamOur']['roles'][-1]; wall['health'] = 250
    p['robot']['roles'] = [unit(100,'bossRobot',9,15,health=800,targetTeam='challenger')]
    t = Turn.load(p); r = fc.predict_wall_risk(t, t.walls()[0], {}, 1)
    assert r['dawn_hold'] and not fc.should_repair_wall(r)
    wall['health'] = 40; t = Turn.load(p)
    assert fc.should_repair_wall(fc.predict_wall_risk(t,t.walls()[0],{},1))


def test_predicted_death_retains_current_attack_only():
    p = arena(129); p['robot']['roles'] = [robot(100,9,15,hp=40)]
    t = Turn.load(p)
    risk = fc.predict_wall_risk(t,t.walls()[0],{100:0},1)
    assert risk['incoming_this_round'] == 5
    assert risk['damage_to_dawn'] == 5


def test_repairer_leaves_second_rocket():
    p = arena(); p['teamOur']['roles'][-1]['health'] = 40
    p['robot']['roles'] = [unit(100,'bossRobot',9,15,health=800,targetTeam='challenger')]
    commands = decide_response(p,Memory())['roleCommandMap']
    assert commands['4']['action'] == 'use' and commands['4']['name'] == 'WallFixer'
    assert all(c.get('controllerId') != '4' for c in commands.values())


def test_gate_never_upgrade_and_no_night_remove():
    p = arena(345); m = Memory(day=3); planner = Planner(Turn.load(p),p,m)
    gate = m.gate_pos
    p['teamOur']['roles'].append(unit(50,'wall',gate.x,gate.y,health=100))
    for u in p['teamOur']['roles']:
        if u['roleType'] == 'worker': u['backpack'] = ['stone','WallUpgradeVoucher1']
    planner = Planner(Turn.load(p),p,m)
    assert not any(o[2] == 50 and 'UpgradeVoucher' in o[3] for o in planner.economic.options())
    assert not any(o[2] == 50 for o in planner.economic.held_options(planner.turn.workers()[0]))
    assert not any(c['action'] in ('remove','build') for c in planner.run().values())


def test_station_and_weapon_priorities():
    p = arena(261); p['teamOur']['roles'][0]['health'] = 1490
    planner = Planner(Turn.load(p),p,Memory(day=3,station_hit=True))
    assert not planner.economic.station_urgent()
    p['teamOur']['roles'][0]['health'] = 800
    planner = Planner(Turn.load(p),p,Memory(day=3))
    assert planner.economic.station_urgent()
    assert planner.economic.options()[0][3].startswith('Station')


def test_no_targets_feasibility_and_boundary():
    p = arena(130); t = Turn.load(p)
    assert fc.remaining_night_rounds(t) == 1
    assert fc.estimate_clear_feasibility(t,t.weapons())['clear_ratio'] == float('inf')
    p['roundNo'] = 131
    assert fc.remaining_night_rounds(Turn.load(p)) == 0


def test_start_of_round_profiles_unchanged_by_planning():
    p = arena(); p['robot']['roles'] = [robot(100,8,16,hp=20),robot(101,12,16,hp=60)]
    t = Turn.load(p); tower = t.weapons()[-1]
    before = fc.raw_damage(t,tower,Pos(12,16))
    fc.plan_fire(t,t.weapons(),{100:0,101:60})
    assert fc.raw_damage(t,tower,Pos(12,16)) == before


def test_sync_thresholds():
    for level,hp in ((2,40),(3,60)):
        p = arena(level=level)
        p['robot']['roles'] = [robot(100+i*3+j,12+i,14+j,hp=hp) for i in range(3) for j in range(3)]
        t = Turn.load(p); rockets = [t for t in t.weapons() if t.kind=='rocket']
        plan, remaining = fc.plan_fire(t,rockets,context={'mode':fc.KILL_ALL_MODE})
        assert len(plan) == 2
        assert sum(v == 0 for v in remaining.values()) >= 8


@pytest.mark.parametrize('ready', [10,11])
def test_one_operator_switches_rockets_without_moving(ready):
    p = arena()
    p['teamOur']['roles'] = [u for u in p['teamOur']['roles'] if u['id'] not in (3,4,12)]
    for u in p['teamOur']['roles']:
        if u['roleType']=='rocket': u['cooldown'] = 0 if u['id']==ready else 2
    p['robot']['roles']=[robot(100,12,15,hp=40)]
    cmd=decide_response(p,Memory())['roleCommandMap']
    assert cmd[str(ready)]['controllerId']=='2'
    assert '2' not in cmd


def test_two_cooling_rockets_do_not_bind_two_operators():
    p=arena()
    for u in p['teamOur']['roles']:
        if u['roleType']=='rocket': u['cooldown']=2
    m=Memory(); decide_response(p,m)
    assert sum(uid in (10,11) for uid in m.tower_assignments.values())<=1


def test_safe_repairer_can_supply_second_rocket_for_threshold():
    p=arena()
    p['robot']['roles']=[robot(100+i*3+j,12+i,14+j,hp=40) for i in range(3) for j in range(3)]
    cmd=decide_response(p,Memory())['roleCommandMap']
    assert cmd['10']['controllerId'] != cmd['11']['controllerId']
    assert '4' in (cmd['10']['controllerId'],cmd['11']['controllerId'])
    assert '4' not in cmd


def test_sparse_wave_preserves_unused_rocket():
    p=arena(); p['robot']['roles']=[robot(100,12,15,hp=20)]
    cmd=decide_response(p,Memory())['roleCommandMap']
    assert sum(uid in ('10','11') for uid in cmd)<=1


@pytest.mark.parametrize('ratio,imminent,pressure,expected', [
    (1.3,False,False,fc.KILL_ALL_MODE),
    (1.1,False,True,fc.SURVIVAL_MODE),
    (10,True,False,fc.SURVIVAL_MODE)])
def test_combat_mode_thresholds(ratio,imminent,pressure,expected):
    assert Memory().choose_combat_mode(Turn.load(arena()),ratio,imminent,pressure)==expected


def test_duplicate_mode_check_does_not_count_twice():
    m=Memory();t=Turn.load(arena())
    for _ in range(4): m.choose_combat_mode(t,.1,False)
    assert m.combat_mode==fc.KILL_ALL_MODE
    assert m.clear_fail_rounds==1


def enclosed(rod=1,stone=True):
    p=arena(260+rod)
    p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['roleType']!='wall']
    m=Memory(day=3); planner=Planner(Turn.load(p),p,m)
    gate=m.gate_pos
    p['teamOur']['roles'][1]['pos']={'x':4,'y':14}
    p['teamOur']['roles'][1]['backpack']=['stone'] if stone else []
    for i,q in enumerate(ring(Turn.load(p),2)):
        p['teamOur']['roles'].append(unit(40+i,'wall',q.x,q.y,health=1000,level=1 if q==gate else 2))
    return p,m


def test_gate_rear_l1_open_confirm_close_before_night():
    p,m=enclosed()
    p['teamOur']['goldNum']=100
    p['weaponShopList']=[{'name':'WeaponUpgradeVoucher1','price':100}]
    pl=Planner(Turn.load(p),p,m);g=m.gate_pos
    assert g.x<pl.turn.station().pos.x
    assert pl.run()['2']['action']=='remove'
    assert m.gate_state=='opening'
    p['roundNo']+=1
    pl=Planner(Turn.load(p),p,m)
    assert pl.run()['2']['action']=='remove'  # failure: retry, not fictional opening
    p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if Pos.load(u['pos'])!=g]
    p['roundNo']=330  # day3, last daytime round
    pl=Planner(Turn.load(p),p,m)
    assert pl.run()['2']=={'action':'build','name':'wall','targetPos':[g.dump()]}
    assert m.gate_state=='closing'
    p['roundNo']+=1
    assert not any(c['action'] in ('build','remove') for c in Planner(Turn.load(p),p,m).run().values())


def test_closed_gate_stays_closed_without_daytime_trip():
    p,m=enclosed()
    cmd=Planner(Turn.load(p),p,m).run()
    assert not any(c['action']=='remove' for c in cmd.values())


def test_rebuild_reassigned_when_original_worker_dies():
    p=arena(140);p['teamOur']['roles'][1]['health']=0
    p['teamOur']['roles'][2]['backpack']=['stone']*3
    m=Memory(day=2);m.wall_rebuilds[2]=Pos(8,16)
    pl=Planner(Turn.load(p),p,m);worker=pl.turn.workers()[0]
    assert pl.economic.maintain_wall(worker,pl.route(worker))
    assert 2 not in m.wall_rebuilds and m.wall_rebuilds[worker.unit_id]==Pos(8,16)


def test_gate_never_seals_worker_outside():
    p,m=enclosed(rod=68)
    p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if Pos.load(u['pos'])!=m.gate_pos]
    p['teamOur']['roles'][2]['pos']={'x':2,'y':14}
    cmd=Planner(Turn.load(p),p,m).run()
    assert not any(c['action']=='build' for c in cmd.values())
    assert cmd['3']['action']=='move'


def test_gate_no_stone_cannot_build():
    p,m=enclosed(rod=70,stone=False)
    p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if Pos.load(u['pos'])!=m.gate_pos]
    assert not any(c['action']=='build' for c in Planner(Turn.load(p),p,m).run().values())


@pytest.mark.parametrize('level',[2,3])
def test_permanent_upgraded_walls_never_demolished(level):
    p=arena(131);wall=p['teamOur']['roles'][-1];wall.update(level=level,health=100)
    p['teamOur']['roles'][1]['backpack']=['stone']*3
    pl=Planner(Turn.load(p),p,Memory(day=2))
    assert not pl.economic.maintain_wall(pl.turn.workers()[0],pl.route(pl.turn.workers()[0]))


def test_l1_daytime_demolition_rebuild_confirms_state():
    p=arena(131);p['teamOur']['roles'][-1]['health']=100
    p['teamOur']['roles'][1]['backpack']=['stone']
    m=Memory(day=2);pl=Planner(Turn.load(p),p,m);r=pl.turn.workers()[0]
    assert pl.economic.maintain_wall(r,pl.route(r))
    assert pl.commands['2']['action']=='remove'
    target=m.wall_rebuilds[2]
    p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['id']!=40]
    p['roundNo']+=1;pl=Planner(Turn.load(p),p,m);r=pl.turn.workers()[0]
    assert pl.economic.maintain_wall(r,pl.route(r))
    assert pl.commands['2']['action']=='build'
    p['teamOur']['roles'].append(unit(45,'wall',target.x,target.y,health=1000))
    p['roundNo']+=1;pl=Planner(Turn.load(p),p,m);r=pl.turn.workers()[0]
    assert not pl.economic.maintain_wall(r,pl.route(r))
    assert 2 not in m.wall_rebuilds


def test_wall_voucher_prevents_unnecessary_demolition():
    p=arena(131);p['teamOur']['roles'][-1]['health']=100
    p['teamOur']['roles'][1]['backpack']=['stone','WallUpgradeVoucher1']
    pl=Planner(Turn.load(p),p,Memory(day=2));r=pl.turn.workers()[0]
    assert not pl.economic.maintain_wall(r,pl.route(r))
    pl.economic.prepare();assert pl.economic.upgrade(r,pl.route(r))
    assert pl.commands['2']['name']=='WallUpgradeVoucher1'


def test_breach_raises_station_budget_priority():
    p=arena(261);p['teamOur']['roles'][0]['health']=1400
    pl=Planner(Turn.load(p),p,Memory(day=3,defense_breached=True))
    assert pl.economic.options()[0][3]=='StationUpgradeVoucher1'


def test_all_l2_before_l3_and_heavy_pressure_rail_override():
    p=arena(261);p['teamOur']['roles'][6]['level']=1
    pl=Planner(Turn.load(p),p,Memory(day=3))
    weapons=[o for o in pl.economic.options() if o[3].startswith('Weapon')]
    assert weapons[0][1]==1
    for u in p['teamOur']['roles']:
        if u['roleType'] in ('rocket','railgun'):u['level']=2
    p['robot']['roles']=[unit(100,'bossRobot',18,15,health=800,targetTeam='challenger')]
    pl=Planner(Turn.load(p),p,Memory(day=3))
    assert next(o for o in pl.economic.options() if o[3].startswith('Weapon'))[4].kind=='railgun'


def test_runtime_duplicate_and_new_match_reset():
    from agent.runtime import Agent
    a=Agent();p=arena();p['robot']['roles']=[robot(100,12,15,hp=800)]
    first=a.respond(p);assert a.respond(p)==first
    before=next(iter(a.sessions.values())).combat_check_round
    assert before==p['roundNo']
    p=fixture();a.respond(p)
    m=next(iter(a.sessions.values()))
    assert m.combat_mode==fc.KILL_ALL_MODE and not m.rocket_hits


def test_robot_power_values_and_survival_reduces_attack_on_kill_only():
    p=arena();p['robot']['roles']=[unit(100,'middleRobot',9,15,health=60,targetTeam='challenger')]
    t=Turn.load(p);ctx=fc.combat_context(t,fc.SURVIVAL_MODE)
    _,chip=fc.apply_damage({100:60},{100:50},{100:1},ctx)
    _,kill=fc.apply_damage({100:60},{100:60},{100:1},ctx)
    assert chip[1]==0 and kill[1]==10
    assert list(fc.POWER.values())==[5,10,20,40]


def test_feasibility_reduces_for_cooldown_and_history():
    p=arena();p['robot']['roles']=[robot(100,12,15,hp=800)]
    t=Turn.load(p);first=fc.estimate_clear_feasibility(t,t.weapons())
    for u in p['teamOur']['roles']:
        if u['roleType']=='rocket':u['cooldown']=3
    t=Turn.load(p);second=fc.estimate_clear_feasibility(t,t.weapons(),[1]*5)
    assert second['clear_ratio']<first['clear_ratio']


@pytest.mark.parametrize('confirmed,loss,expected',[(True,40,[20]),(False,40,[]),(None,40,[]),(True,50,[])])
def test_rocket_history_requires_attributed_observation(confirmed,loss,expected):
    p=arena();p['robot']['roles']=[robot(100,12,13,hp=500)]
    m=Memory();t=Turn.load(p);m.observe(t,p)
    m.remember(t,{'roleCommandMap':{'10':{'action':'attack','controllerId':'2',
                 'targetPos':[{'x':12,'y':13}]*2}}})
    assert not m.rocket_hits
    p['roundNo']+=1;p['robot']['roles'][0]['health']-=loss
    p['lastRoundRoleActionResults']={} if confirmed is None else {'10':confirmed}
    m.observe(Turn.load(p),p)
    assert m.rocket_hits==expected
    m.observe(Turn.load(p),p)
    assert m.rocket_hits==expected


def test_dense_chip_damage_does_not_justify_second_rocket():
    p=arena();p['robot']['roles']=[robot(100+i,12,13+i,hp=800) for i in range(3)]
    t=Turn.load(p);single={10:[Pos(12,14)]*2};double={**single,11:[Pos(12,14)]*2}
    assert not fc.rocket_sync_value(t,t.weapons(),single,double)


def test_dead_operator_cannot_be_controller():
    p=arena();p['teamOur']['roles'][1]['health']=0
    p['robot']['roles']=[robot(100,12,15,hp=40)]
    result=decide_response(p,Memory())['roleCommandMap']
    assert all(c.get('controllerId')!='2' for c in result.values())


def test_final_night_damage_is_observed_at_dawn():
    p=arena(130);m=Memory();m.observe(Turn.load(p),p)
    p['roundNo']=131
    p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['id']!=40]
    p['teamOur']['roles'][0]['health']=1400
    m.observe(Turn.load(p),p)
    assert m.defense_breached


@pytest.mark.parametrize('rod,remaining', [(70,0),(71,60),(129,2),(130,1),(131,0)])
def test_day_night_boundary_counts(rod,remaining):
    assert fc.remaining_night_rounds(Turn.load(arena(rod)))==remaining


def test_gate_does_not_upgrade_through_stale_delivery_job():
    p,m=enclosed()
    wall=next(w for w in Turn.load(p).walls() if w.pos==m.gate_pos)
    m.jobs[2]={'type':'upgrade','unit':wall.unit_id,'item':'WallUpgradeVoucher1',
               'level':1,'bought':True,'price':0}
    p['teamOur']['roles'][1]['backpack'].append('WallUpgradeVoucher1')
    pl=Planner(Turn.load(p),p,m);pl.economic.prepare()
    assert m.jobs.get(2,{}).get('unit')!=wall.unit_id


@pytest.mark.parametrize('mirror',[False,True])
def test_day3_constructs_complete_ring_and_gate_with_available_stone(mirror):
    from test_strategy import apply_construction
    p=arena(261)
    p['teamOur']['roles']=[u for u in p['teamOur']['roles'] if u['roleType']!='wall']
    for u in p['teamOur']['roles']:
        if u['roleType']=='worker':u['backpack']=['stone']*25
        # The legacy rail at (5,16) isolates the southeast interior pocket
        # once the ring closes. A full-ring fixture needs connected seats.
        if u['roleType']=='railgun':u['pos']={'x':4,'y':13}
    if mirror:
        for u in p['teamOur']['roles']:
            u['pos']['x']=(39 if u['roleType']=='station' else 40)-u['pos']['x']
    m=Memory()
    for _ in range(70):
        commands=decide_response(p,m)['roleCommandMap']
        apply_construction(p,commands)
    t=Turn.load(p)
    assert len(t.walls())==20
    gate=next(w for w in t.walls() if w.pos==m.gate_pos)
    assert gate.level==1
    assert all(min(distance(r.pos,q) for q in t.footprint(t.station()))<=1 for r in t.controllable())


def test_survival_targets_imminent_attack_over_distant_damage():
    p=arena()
    p['teamOur']['roles'][-1]['health']=30
    p['robot']['roles']=[unit(100,'middleRobot',9,15,health=20,targetTeam='challenger')]
    p['robot']['roles'] += [robot(200+i*3+j,15+i,11+j,hp=500) for i in range(3) for j in range(3)]
    t=Turn.load(p)
    _,res=fc.plan_fire(t,[next(x for x in t.weapons() if x.unit_id==10)],context={'mode':fc.SURVIVAL_MODE})
    assert res[100]==0


def test_no_repair_pack_still_produces_legal_defense():
    p=arena();p['teamOur']['roles'][-1]['health']=30
    p['teamOur']['roles'][3]['backpack']=[]
    p['robot']['roles']=[unit(100,'bossRobot',9,15,health=800,targetTeam='challenger')]
    commands=decide_response(p,Memory())['roleCommandMap']
    controllers=[c['controllerId'] for c in commands.values() if c['action']=='attack']
    assert len(controllers)==len(set(controllers))
    assert not any(c.get('name')=='WallFixer' for c in commands.values())


def test_front_then_side_then_back_upgrades_grouped_by_level():
    p,m=enclosed();p['roundNo']=261
    pl=Planner(Turn.load(p),p,m)
    walls=[o for o in pl.economic.options() if o[4].kind=='wall']
    groups=[pl.wall_group(o[4].pos) for o in walls]
    assert groups==sorted(groups)
    assert 3 not in groups

