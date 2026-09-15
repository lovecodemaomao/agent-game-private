"""Pure, bounded fleet fire planning against the wave attacking our base.

All ballistic profiles use start-of-round health: predicted kills do not remove
blockers before simultaneous damage resolves. Residual HP only scores useful damage.
"""
from itertools import permutations
from .grid import neighbours
from .protocol import Pos, distance, station_footprint

BEAM_WIDTH = 8
KILL_ALL_MODE = 'KILL_ALL'
SURVIVAL_MODE = 'SURVIVAL'
CLEAR_SAFE_RATIO = 1.20
CLEAR_FAIL_RATIO = 1.00
RISK_MARGIN_RATIO = .20
RISK_MARGIN_HP = 20
ROCKET_PERIOD = 4
HIT_HISTORY_SIZE = 5
POWER = {'smallRobot': 5, 'middleRobot': 10, 'largeRobot': 20, 'bossRobot': 40}


def remaining_night_rounds(turn):
    return 0 if turn.is_day else 130 - (turn.round_no - 1) % 130


def robot_power(robot):
    return POWER.get(robot.kind, 5)


def structure_eta(turn, robot, structure):
    cells = station_footprint(structure.pos) if structure.kind == 'station' else (structure.pos,)
    return max(0, min(distance(robot.pos, p) for p in cells) - 1)


def threatened_structure(turn, robot):
    """First barrier on a straight approach; routing is a conservative estimate.

    Adjacent walls take precedence. An unblocked approach threatens the base,
    not an arbitrary far wall. Exact official robot path selection is unknown.
    """
    station = turn.station()
    if station is None:
        return None
    adjacent = [w for w in turn.walls() if distance(w.pos, robot.pos) <= 1]
    if adjacent:
        return min(adjacent, key=lambda w: (w.health, w.unit_id))
    hits = [(entry, w.unit_id, w) for w in turn.walls()
            for end in station_footprint(station.pos)
            if (entry := on_segment(robot.pos, end, w.pos)) is not None]
    return min(hits, key=lambda x: x[:2])[2] if hits else station


def safety_margin(damage):
    return max(RISK_MARGIN_HP, damage * RISK_MARGIN_RATIO)


def predict_wall_risk(turn, wall, remaining=None, repair_eta=1, threats=None):
    """Upper bound: attacking every round; no credit for unconfirmed stun/cooldown.

    Same-round incoming attacks survive predicted kills. Subsequent attacks may
    use predicted survivors. The immutable turn is never edited.
    """
    remaining = remaining or {}
    horizon = remaining_night_rounds(turn)
    before = min(horizon, max(2, int(repair_eta) + 1))
    now = soon = dawn = 0
    threats = threats if threats is not None else {
        r.robot_id: threatened_structure(turn, r) for r in turn.robots
        if r.health > 0 and our_target(turn, r)}
    for r in turn.robots:
        if r.health <= 0 or not our_target(turn, r):
            continue
        target = threats.get(r.robot_id)
        if target is None or target.unit_id != wall.unit_id:
            continue
        eta = structure_eta(turn, r, wall)
        power = robot_power(r)
        alive = remaining.get(r.robot_id, r.health) > 0
        current = int(eta == 0 and horizon > 0)
        now += power * current
        soon += power * (max(0, before-eta) if alive else current)
        dawn += power * (max(0, horizon-eta) if alive else current)
    return {'unit_id': wall.unit_id, 'current_hp': wall.health,
            'incoming_this_round': now, 'damage_before_repair': soon,
            'damage_to_dawn': dawn, 'remaining_night_rounds': horizon,
            'dawn_hold': wall.health > dawn + safety_margin(dawn),
            'imminent': soon > 0 and wall.health <= soon + safety_margin(soon)}


def estimate_wall_damage_to_dawn(turn, wall, remaining=None):
    return predict_wall_risk(turn, wall, remaining)['damage_to_dawn']


def should_repair_wall(risk):
    return not risk['dawn_hold'] and risk['imminent']


def combat_context(turn, mode=KILL_ALL_MODE):
    targets = {r.robot_id: threatened_structure(turn, r) for r in turn.robots
               if r.health > 0 and our_target(turn, r)}
    risks = {u.unit_id: predict_wall_risk(turn,u,threats=targets)
             for u in (*turn.walls(), turn.station()) if u is not None}
    urgency = {}
    for r in turn.robots:
        target = targets.get(r.robot_id)
        if target is not None:
            eta = structure_eta(turn,r,target)
            urgency[r.robot_id] = (int(eta <= 1 and risks[target.unit_id]['imminent']),
                                  robot_power(r) if eta <= 1 else 0,
                                  robot_power(r)/(1+eta))
    return {'mode': mode, 'urgency': urgency, 'risks': risks, 'threats': targets}


def estimate_clear_feasibility(turn, towers, history=(), operator_delays=None):
    eligible = {r.robot_id for r in turn.robots if r.health > 0 and our_target(turn,r)}
    hp = sum(r.health for r in turn.robots if r.robot_id in eligible)
    night = remaining_night_rounds(turn)
    rocket_damage = rail_damage = 0.0
    delays = operator_delays or {}
    for tower in towers:
        if tower.health <= 0:
            continue
        time = max(0, night-max(tower.cooldown, delays.get(tower.unit_id, 0)))
        shots = candidate_shots(turn,tower,eligible)
        effective = max((sum(min(r.health,profile.get(r.robot_id,0))
                             for r in turn.robots if r.robot_id in eligible)
                         for _,profile in shots), default=0)
        if tower.kind == 'rocket':
            if history:
                effective = min(effective, sum(history[-HIT_HISTORY_SIZE:])/len(history[-HIT_HISTORY_SIZE:]))
            rocket_damage += ((time+ROCKET_PERIOD-1)//ROCKET_PERIOD)*max(1,tower.level)*effective
        else:
            rail_damage += time*effective
    return {'remaining_effective_hp': hp, 'future_rocket_effective_damage': rocket_damage,
            'future_rail_damage': rail_damage,
            'clear_ratio': (rocket_damage+rail_damage)/hp if hp else float('inf')}


def our_target(turn, robot):
    if robot.target_team in ('challenger', 'defender'):
        return robot.target_team == turn.team_type
    station = turn.station()
    if station is None:
        return False
    enemy = next((u for u in turn.enemies if u.kind == 'station'), None)
    other = enemy.pos if enemy else Pos(turn.width - 2 - station.pos.x, station.pos.y)
    own_distance = min(distance(robot.pos, p) for p in station_footprint(station.pos))
    enemy_distance = min(distance(robot.pos, p) for p in station_footprint(other))
    if own_distance != enemy_distance:
        return own_distance < enemy_distance
    # Ambiguous high/low positions: prefer our half, never guess across mid-map.
    midpoint = (station.pos.x + other.x + 1) / 2
    return robot.pos.x < midpoint if station.pos.x < other.x else robot.pos.x > midpoint


def threat(turn, robot):
    if not our_target(turn, robot):
        return 0.0
    station = turn.station()
    d = min(distance(robot.pos, p) for p in station_footprint(station.pos))
    power = {'smallRobot': 5, 'middleRobot': 10, 'largeRobot': 20, 'bossRobot': 40}.get(robot.kind, 5)
    # Lane alignment is a soft preference; explicit own-wave flankers stay eligible.
    lane = 1 + 0.2 / (1 + abs(robot.pos.y - (station.pos.y - 0.5)))
    return (1 + power / 20 + 6 / max(1, d - 2)) * lane


def on_segment(start, end, point):
    low, high = 0.0, 1.0
    for a, b, c in ((start.x, end.x, point.x), (start.y, end.y, point.y)):
        delta = b - a
        if delta == 0:
            if abs(a - c) > 0.5:
                return None
        else:
            left, right = sorted(((c - 0.5 - a) / delta, (c + 0.5 - a) / delta))
            low, high = max(low, left), min(high, right)
            if low > high:
                return None
    return low


def raw_damage(turn, tower, target):
    robots = [r for r in turn.robots if r.health > 0]
    if tower.kind == 'rocket':
        return {r.robot_id: 20 if r.pos == target else 10
                for r in robots if distance(r.pos, target) <= 1}
    hits = []
    for robot in robots:
        entry = on_segment(tower.pos, target, robot.pos)
        if entry is not None:
            hits.append((entry, robot.robot_id, robot.health))
    energy = 10 * max(1, tower.level) if tower.kind == 'railgun' else 10
    damage = {}
    for _, rid, health in sorted(hits):
        dealt = min(health, energy) if tower.kind == 'railgun' else energy
        damage[rid] = dealt
        energy -= dealt
        if tower.kind != 'railgun' or energy <= 0:
            break
    return damage


def shot_damage(turn, tower, target, remaining):
    return {rid: min(remaining.get(rid, 0), value)
            for rid, value in raw_damage(turn, tower, target).items()}


def candidate_shots(turn, tower, eligible):
    points = set()
    for robot in turn.robots:
        if robot.robot_id not in eligible:
            continue
        points.add(robot.pos)
        if tower.kind == 'rocket':
            points.update(neighbours(robot.pos))
    shots = []
    for point in sorted(points, key=lambda p: (p.x, p.y)):
        if not (0 <= point.x < turn.width and 0 <= point.y < turn.height):
            continue
        if distance(tower.pos, point) > tower.range_of_attack():
            continue
        profile = raw_damage(turn, tower, point)
        if any(profile.get(rid, 0) > 0 for rid in eligible):
            shots.append((point, profile))
    return shots


def apply_damage(remaining, profile, weights, context=None):
    result = remaining.copy()
    useful = kills = waste = collateral = score = 0
    for rid, damage in profile.items():
        if rid not in weights:
            collateral += damage
            continue
        dealt = min(result[rid], damage)
        killed = int(result[rid] > 0 and dealt == result[rid])
        result[rid] -= dealt
        useful += dealt
        kills += killed
        waste += damage - dealt
        score += (dealt + 4 * killed) * weights[rid]
    if context and context.get('mode') == SURVIVAL_MODE:
        removed = [context.get('urgency', {}).get(rid, (0,0,0))
                   for rid in profile if rid in weights and remaining[rid] > 0 and result[rid] == 0]
        danger, attack, eta = (sum(x[i] for x in removed) for i in range(3))
        return result, (danger, attack, eta, kills, useful, -waste, -collateral)
    return result, (kills, useful, -waste, score, -collateral, 0, 0)


def volley(tower, shots, initial, weights, context=None, alternatives=False):
    count = min(3, max(1, tower.level)) if tower.kind in ('rocket', 'gatling') else 1
    # Maximize actual own-wave damage; equal damage prefers no help to the other
    # team, less overkill, then proximity/power/lane threat and completed kills.
    beam = [(initial, (), (0,) * 7)]
    for _ in range(count):
        children = []
        for remaining, targets, value in beam:
            for point, profile in shots:
                if tower.kind == 'gatling' and any(
                        (point.x-tower.pos.x)*(p.x-tower.pos.x) +
                        (point.y-tower.pos.y)*(p.y-tower.pos.y) < 0 for p in targets):
                    continue
                after, gain = apply_damage(remaining, profile, weights, context)
                children.append((after, targets + (point,), tuple(a+b for a,b in zip(value,gain))))
        if not children:
            empty = (initial, (), (0,) * 7)
            return [empty] if alternatives else empty
        children.sort(key=lambda item: item[2], reverse=True)
        # For rockets/railguns future choices depend on damage, not target order.
        # Gatling also retains its cone constraints in the deduplication key.
        beam, seen = [], set()
        for child in children:
            key = (tuple(child[0].items()),
                   frozenset(child[1]) if tower.kind == 'gatling' else None)
            if key in seen:
                continue
            seen.add(key)
            beam.append(child)
            if len(beam) == BEAM_WIDTH:
                break
    return beam if alternatives else beam[0]


def plan_fire(turn, towers, remaining=None, context=None):
    context = context or combat_context(turn)
    if 'urgency' not in context:
        context = combat_context(turn, context.get('mode', KILL_ALL_MODE))
    weights = {r.robot_id: threat(turn, r) for r in turn.robots
               if r.health > 0 and our_target(turn, r)}
    initial = {rid: (remaining.get(rid, 0) if remaining is not None else
                    next(r.health for r in turn.robots if r.robot_id == rid)) for rid in weights}
    profiles = {t.unit_id: candidate_shots(turn, t, weights) for t in towers
                if t.cooldown == 0 and t.health > 0}
    available = sorted((t for t in towers if profiles.get(t.unit_id)), key=lambda t:t.unit_id)
    best_plan, best_remaining, best_score = {}, initial, (0,) * 8
    # At most three towers: compare all six planning orders, including restricted
    # range weapons first. This is bounded search, not a global optimality claim.
    for order in permutations(available):
        fleet_beam = [(initial,{},(0,)*7)]
        for tower in order:
            children = list(fleet_beam)  # Explicit hold-fire candidate.
            for residual,plan,total in fleet_beam:
                for after,targets,value in volley(tower,profiles[tower.unit_id],residual,weights,context,True):
                    if sum(residual.values()) > sum(after.values()):
                        children.append((after,{**plan,tower.unit_id:list(targets)},
                                         tuple(a+b for a,b in zip(total,value))))
            children.sort(key=lambda x:x[2]+(-len(x[1]),),reverse=True)
            fleet_beam,seen = [],set()
            for child in children:
                key = tuple(sorted(child[0].items()))
                if key not in seen:
                    seen.add(key); fleet_beam.append(child)
                if len(fleet_beam) == 4:
                    break
        for residual,plan,total in fleet_beam:
            score = total + (-len(plan),)
            if score > best_score:
                best_plan, best_remaining, best_score = plan, residual, score
    # Preserve combined kill-threshold candidates that a per-tower beam would
    # prune (one rocket may prefer a few immediate kills over setting up 9).
    rockets = [t for t in available if t.kind=='rocket']
    if len(rockets) == 2:
        maps = [{p:d for p,d in profiles[t.unit_id]} for t in rockets]
        for point in sorted(maps[0].keys() & maps[1].keys(),key=lambda p:(p.x,p.y)):
            residual, plan, total = initial, {}, (0,) * 7
            for t,profile in zip(rockets,maps):
                plan[t.unit_id] = [point]*max(1,min(3,t.level))
                for _ in plan[t.unit_id]:
                    residual,value = apply_damage(residual,profile[point],weights,context)
                    total = tuple(a+b for a,b in zip(total,value))
            for t in available:
                if t in rockets:
                    continue
                after,targets,value = volley(t,profiles[t.unit_id],residual,weights,context)
                if sum(after.values()) < sum(residual.values()):
                    residual=after; plan[t.unit_id]=list(targets)
                    total=tuple(a+b for a,b in zip(total,value))
            score = total+(-len(plan),)
            if score > best_score:
                best_plan,best_remaining,best_score = plan,residual,score
    return best_plan, best_remaining


def score_plan(turn, towers, plan, context=None):
    context = context or combat_context(turn)
    weights = {r.robot_id: threat(turn,r) for r in turn.robots if r.health > 0 and our_target(turn,r)}
    residual = {r.robot_id:r.health for r in turn.robots if r.robot_id in weights}
    total = (0,) * 7
    for tower in sorted(towers,key=lambda t:t.unit_id):
        for point in plan.get(tower.unit_id, ()):
            residual, value = apply_damage(residual,raw_damage(turn,tower,point),weights,context)
            total = tuple(a+b for a,b in zip(total,value))
    return total + (-sum(t.kind == 'rocket' and t.unit_id in plan for t in towers),)


def rocket_sync_value(turn, towers, single, double, context=None):
    """Sync only for extra kills or reduced imminent pressure, not raw chip damage."""
    def metrics(plan):
        damage = {}
        for t in towers:
            for point in plan.get(t.unit_id, ()):
                for rid, value in raw_damage(turn,t,point).items():
                    damage[rid] = damage.get(rid,0)+value
        return {r.robot_id for r in turn.robots if our_target(turn,r) and r.health > 0
                and damage.get(r.robot_id,0) >= r.health}
    first, both = metrics(single), metrics(double)
    if len(both) > len(first):
        return True
    context = context or combat_context(turn)
    def removed_power(kills):
        return sum(context['urgency'].get(uid,(0,0,0))[1] for uid in kills)
    return removed_power(both) > removed_power(first)


def select_targets(turn, tower, remaining):
    plan, after = plan_fire(turn, [tower], remaining)
    remaining.update(after)
    return plan.get(tower.unit_id, [])
