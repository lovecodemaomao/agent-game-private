"""Persistent procurement and complete mine -> vendor -> home route scoring."""
from collections import Counter
from .geography import INF
from .protocol import distance
from . import fire_control as combat

STATION_CRITICAL_RATIO = .60
STATION_MODERATE_RATIO = .80
HEAVY_HP_RATIO = .50
L1_REBUILD_RATIO = .50

ORES=('stone','iron','copper')
SELLABLE=('iron','copper')                 # 需求5: 石头不卖, 只用于建墙/修墙(重建)
STONE='stone'
SUMMON_ORDER='LargeRobotSummonOrder'      # 第3天起、防线达标后才采购
SUMMON_ORDER_TRIGGER=100
JOURNEY_MARGIN=5                          # 采购/采矿完整行程的保守余量
HARVEST_BUFFER=2                          # 顺手采集只留 2 回合缓冲（返程已在 margin 内）
NEAR_HOME_RADIUS=4                        # "家附近"判定半径（基地切比雪夫距离）
HARVEST_DETOUR=2                          # 顺路判据: 多绕不超过 2 回合即视为顺手
WALL_FIXER_RESERVE=3                      # 常备修复包数量上限
# 每日配额控制批量采购与维修储备；武器升级排序始终优先于围墙升级
#   day1: 建完半圈围墙(石头) + 两个武器升级
#   day2: 4 个前挡围墙升级 + 一个武器升2级
#   day3: 4 个前挡升2级 + 两个武器升2级
DAY_PLAN={1:{'front_walls':0,'weapons':2},
          2:{'front_walls':4,'weapons':1},
          3:{'front_walls':4,'weapons':2}}
DAY_PLAN_DEFAULT={'front_walls':4,'weapons':2}
DAY1_ORE_PHASE_ROUNDS=30                  # 第1天先赚钱: 前30回合全员采铁/铜, 之后再采石修墙
DAY_ROUNDS=70                            # 白天回合数(夜间预置站位按整日预算选矿)
SPEND_FIXER=10                            # 回家前清钱: 修复包价
SPEND_TRIP_SLACK=3                        # 清钱时点余量(回合)
STATION_FALLBACK_HP=1000                  # 保留: 旧兜底阈值(现由"受伤即优先"取代)
STATION_FALLBACK_DAY=4                    # 保留: 旧兜底天数
WALL_MAINTENANCE_DAY=3                   # 需求5: 第3天起掉血的墙要重建/修复
DAY3_WALL_DAMAGE_RATIO=0.5               # 需求5: 第3天只看"掉血超过 50%"的墙(修复包)
STONE_MORNING_ROUNDS=40                  # 石头只在当天早上(前 40 回合)安排采集, 其余时间采矿
STATION_URGENT_DAY=3                      # 需求3: 第3天起, 基地一受伤就优先买基地升级券
STATION_TIER2_GOLD=150                    # 需求3: 白天开始时金币>150 且已2级 -> 直接上2级券
FRONT_REPAIR_RATIO=0.7                    # 面向机器人的前排墙: 血量低于该比例即主动修复
ANY_REPAIR_RATIO=0.5                      # 其余围墙: 低于该比例即修复
CRITICAL_REPAIR_RATIO=0.25                # 濒临被打爆: 即使要现买修复包也优先于升级
MIN_SELL_BATCH=6                          # 至少攒够这么多才值得跑一趟小贩
HP={'station':(1500,3000,4500),'wall':(1000,1500,2000),
    'rocket':(1000,1500,2000),'railgun':(1000,1500,2000),'gatling':(1000,1500,2000)}


class Economy:
    def __init__(self,p):
        self.p=p; self.turn=p.turn; self.m=p.memory
        self.mine_claims=Counter()

    def plan(self):
        return DAY_PLAN.get(self.m.day or ((self.turn.round_no-1)//130+1), DAY_PLAN_DEFAULT)

    def repair_threshold(self,wall):
        """围墙修复阈值: 第1-2天只有明显受损才修; 第3天起只看掉血超过 50% 的墙(需求5改)。"""
        if (self.m.day or ((self.turn.round_no-1)//130+1))>=WALL_MAINTENANCE_DAY:
            return DAY3_WALL_DAMAGE_RATIO
        return FRONT_REPAIR_RATIO if wall.pos in self.p.walls else ANY_REPAIR_RATIO

    def damaged_walls(self):
        """低于修复阈值的围墙（前排墙阈值更宽松; 第3天起掉血即为受损）。"""
        out=[]
        for w in self.turn.walls():
            level=max(1,min(3,w.level)); ratio=w.health/HP['wall'][level-1]
            if ratio<self.repair_threshold(w): out.append(w)
        return out

    def station_urgent(self):
        station = self.turn.station()
        if station is None or station.level >= 3 or station.health <= 0:
            return False
        ratio = station.health / HP['station'][max(1,station.level)-1]
        return ratio < STATION_CRITICAL_RATIO or self.m.defense_breached

    def weapon_priority(self, weapon):
        # All three L2 before any L3; large/boss pressure may advance the rail.
        level = max(1, weapon.level)
        targets = [r for r in self.turn.robots if r.health > 0 and combat.our_target(self.turn,r)]
        heavy = sum(r.health for r in targets if r.kind in ('largeRobot','bossRobot'))
        rail_first = bool(targets) and heavy >= sum(r.health for r in targets)*HEAVY_HP_RATIO
        if level == 1:
            return .5 if weapon.kind == 'rocket' else .8
        return 1.0 if weapon.kind == ('railgun' if rail_first else 'rocket') else 1.2

    def station_tier(self):
        """基地升级券档位: 白天开始时金币>150 且已 2 级 -> 2 级券, 其余买当前等级。"""
        station=self.turn.station()
        level=max(1,min(3,station.level)) if station is not None else 1
        if self.m.day_start_gold>STATION_TIER2_GOLD and level>=2: return 2
        return level

    def wall_voucher_first(self):
        """围墙升级券是否优先于武器券(用户最新口径)。

        **无论怎样都是优先升级武器**；只有一种例外: 前夜围墙承伤超过 80%
        (见 WALL_PRESSURE_RATIO)时, 当天降武器升级、把围墙升级券提到前面。
        基地受伤时基地升级券仍是第一优先级(见 options() 的 priority=0 分支)。
        """
        return self.wall_pressure_high()

    def wall_pressure_high(self):
        """需求6: 第3天起, 若前一晚围墙承伤超过 50%, 当天武器升级让位给围墙升级券。"""
        if not self.turn.walls(): return False
        return bool(self.m.wall_pressure_high)

    def fixer_targets(self):
        """需要修复包的受损墙: 第3天起只有 2/3 级墙(1 级带伤走"拆掉重建", 石头免费)。"""
        out=[]
        for w in self.damaged_walls():
            if max(1,min(3,w.level))>=2:
                out.append(w)
            elif (self.m.day or ((self.turn.round_no-1)//130+1))<WALL_MAINTENANCE_DAY:
                out.append(w)      # 第3天之前还没有重建机制, 只能靠修复包
        return out

    def wall_quota_left(self):
        """当天还需完成的围墙升级数量（没有可升级的围墙时视为已完成）。"""
        upgradable=any(max(1,min(3,w.level))<2 for w in self.turn.walls())
        if not upgradable: return 0
        return max(0, self.plan()['front_walls'] - self.m.day_wall_upgrades)

    def weapon_quota_left(self):
        """当天还需完成的武器升级数量。"""
        if not any(w.level < 3 for w in self.turn.weapons()):
            return 0
        return max(0, self.plan()['weapons'] - self.m.day_weapon_upgrades)

    def wall_phase_active(self):
        """迎敌半圈围墙是否仍处于"补到2级"的阶段（未达半数即为是）。"""
        walls=self.turn.walls()
        if not walls: return False
        done=sum(1 for w in walls if max(1,min(3,w.level))>=2)
        return done < max(1,len(walls)//2)

    def options(self):
        options = []
        for u in (*self.turn.weapons(), *self.turn.walls(), self.turn.station()):
            if u is None or u.health <= 0:
                continue
            level = max(1,min(3,u.level))
            ratio = u.health / HP[u.kind][level-1]
            if u.kind == 'wall':
                if u.pos == self.m.gate_pos:
                    continue
                group = self.p.wall_group(u.pos)
                if level < 3:
                    # Group first, then level: a complete front tier before side/back.
                    priority = (0.9 if self.wall_voucher_first() else 2.0) + group*2 + (level-1)*.4
                    item = f'WallUpgradeVoucher{level}'
                elif ratio < self.repair_threshold(u):
                    item = 'WallFixer'; priority = 3.0+group
                else:
                    continue
            elif u.kind == 'station':
                if level >= 3:
                    continue
                item = f'StationUpgradeVoucher{level}'
                priority = 0 if self.station_urgent() else 1.4 if ratio < STATION_MODERATE_RATIO else 6
            else:
                if level >= 3:
                    continue
                item = f'WeaponUpgradeVoucher{level}'
                priority = self.weapon_priority(u) + (1 if self.wall_voucher_first() else 0)
            options.append((priority,level,u.unit_id,item,u))
        return sorted(options,key=lambda x:x[:3])

    def maintain_wall(self, role, routes):
        if not self.turn.is_day:
            return False
        rod = (self.turn.round_no-1)%130+1
        workers = {w.unit_id:w for w in self.turn.workers()}
        if role.unit_id not in self.m.wall_rebuilds and 'stone' in role.backpack:
            for uid,pos in list(self.m.wall_rebuilds.items()):
                owner = workers.get(uid)
                if (owner is None or 'stone' not in owner.backpack) and routes.distance(pos)<10**6:
                    self.m.wall_rebuilds[role.unit_id] = self.m.wall_rebuilds.pop(uid)
                    break
        # Finish a previously observed demolition before taking any other job.
        pending = self.m.wall_rebuilds.get(role.unit_id)
        if pending is not None:
            if any(w.pos == pending for w in self.turn.walls()):
                wall = next(w for w in self.turn.walls() if w.pos == pending)
                if wall.health >= HP['wall'][max(1,wall.level)-1] or wall.level > 1:
                    self.m.wall_rebuilds.pop(role.unit_id,None)
                    return False
                # Failed remove: keep the same transaction, never assume success.
                if rod < 69 and 'stone' in role.backpack:
                    return self.p.interact(role,routes,pending,'remove',targetPos=[pending.dump()])
                return False
            if 'stone' in role.backpack and rod <= 70 and pending not in self.p.build_targets:
                if self.p.interact(role,routes,pending,'build',name='wall',targetPos=[pending.dump()]):
                    self.p.build_targets.add(pending)
                    return True
            return False
        occupied = set(self.m.wall_rebuilds.values())
        for wall in sorted(self.turn.walls(),key=lambda w:(self.p.wall_group(w.pos),w.health,w.unit_id)):
            if wall.level != 1 or wall.health >= HP['wall'][0]*L1_REBUILD_RATIO or wall.pos in occupied:
                continue
            if wall.pos == self.m.gate_pos:
                continue
            voucher = 'WallUpgradeVoucher1'
            delivery = any(j.get('unit')==wall.unit_id and j.get('item')==voucher
                           and j.get('bought') and uid in workers
                           and voucher in workers[uid].backpack
                           and self.p.route(workers[uid]).distance(wall.pos)+1<=71-rod
                           for uid,j in self.m.jobs.items())
            if voucher in role.backpack or delivery:
                continue
            reserve = int(self.p.day>=3 and not self.p.gate_wall())
            if role.backpack.count('stone') <= reserve or routes.distance(wall.pos)+2 > 70-rod:
                continue
            if self.p.interact(role,routes,wall.pos,'remove',targetPos=[wall.pos.dump()]):
                self.m.wall_rebuilds[role.unit_id] = wall.pos
                return True
        return False

    def target(self,job):
        return next((u for u in self.turn.ours if u.unit_id==job.get('unit') and u.health>0),None)

    def usable(self,job,target):
        if target is None: return False
        if target.kind=='wall' and target.pos==self.m.gate_pos: return False
        if job['item']=='WallFixer' and not self.turn.is_day:
            return combat.should_repair_wall(combat.predict_wall_risk(self.turn,target))
        if job['item']=='WallFixer': return target.health<HP['wall'][max(1,target.level)-1]
        return target.level==job['level']

    def held_options(self, role):
        """Inventory use is independent of the shop's preferred repair option."""
        result=[]
        for target in self.turn.ours:
            if target.health<=0 or target.kind not in HP: continue
            if target.kind=='wall' and target.pos==self.m.gate_pos: continue
            level=max(1,min(3,target.level))
            prefix=('Station' if target.kind=='station' else
                    'Wall' if target.kind=='wall' else 'Weapon')
            voucher=f'{prefix}UpgradeVoucher{level}'
            if level<3 and voucher in role.backpack:
                result.append((0,level,target.unit_id,voucher,target))
            if (target.kind=='wall' and 'WallFixer' in role.backpack
                    and ((not self.turn.is_day and combat.should_repair_wall(combat.predict_wall_risk(self.turn,target)))
                         or (self.turn.is_day and level>=2 and target in self.damaged_walls()))):
                result.append((1,level,target.unit_id,'WallFixer',target))
        return result

    def use_consumable(self, role):
        if 'Medicine' in role.backpack and role.health < 100:
            self.p.commands[str(role.unit_id)]={'action':'use','name':'Medicine'}
            return True
        if SUMMON_ORDER in role.backpack:
            self.p.commands[str(role.unit_id)]={'action':'use','name':SUMMON_ORDER}
            if self.m.jobs.get(role.unit_id,{}).get('item') == SUMMON_ORDER:
                self.m.jobs.pop(role.unit_id,None)
            self.m.summon_order_done=True
            return True
        return False

    def defer_delivery(self, role, job):
        self.m.blocked_deliveries[(role.unit_id,job['unit'])]=self.turn.round_no+8
        self.m.jobs.pop(role.unit_id,None)

    def purchase_count(self, role, item, price, requested, assigned=()):
        """Bound a batch by stock, matching targets, capacity and current funds."""
        capacity=(role.capacity if role.capacity is not None else 100)-len(role.backpack)
        limit=min(requested,max(0,capacity),int(self.p.gold//price)) if price>0 else 0
        if 'UpgradeVoucher' in item:
            prefix,level=item.split('UpgradeVoucher')
            kinds={'Weapon':('rocket','railgun','gatling'),'Wall':('wall',),'Station':('station',)}
            targets=sum(u.health>0 and u.kind in kinds.get(prefix,()) and u.level==int(level)
                        and u.unit_id not in assigned
                        and not (u.kind=='wall' and u.pos==self.m.gate_pos)
                        and not (prefix=='Wall' and int(level)==1 and u.health<=0)
                        for u in self.turn.ours)
            held=sum(r.backpack.count(item) for r in self.turn.controllable())
            limit=min(limit,max(0,targets-held))
        return max(0,limit)

    def prepare(self):
        for role in self.turn.controllable():
            job=self.m.jobs.get(role.unit_id)
            if job and job.get('type')=='upgrade':
                if not self.usable(job,self.target(job)):
                    self.m.jobs.pop(role.unit_id,None)
                else:
                    job['bought']=job['item'] in role.backpack
                    if self.p.route(role).distance(self.target(job).pos)>=INF:
                        self.defer_delivery(role,job)
                        continue
                    if not job['bought'] and (not self.turn.is_day or self.p.remaining<8):
                        self.m.jobs.pop(role.unit_id,None)
                    elif not job['bought'] and any(
                            self.p.route(role).distance(o[4].pos)<INF and
                            self.m.blocked_deliveries.get((role.unit_id,o[2]),0)<=self.turn.round_no
                            for o in self.held_options(role)):
                        self.m.jobs.pop(role.unit_id,None)
        occupied={j['unit'] for j in self.m.jobs.values() if j.get('type')=='upgrade'}
        # Recover vouchers already held, including after a worker was revived.
        # Bind vouchers before repair packs, so an upgrade can restore health
        # without consuming a second role's repair stock on the same building.
        for role in sorted(self.turn.controllable(),key=lambda r:
                           not any('UpgradeVoucher' in item for item in r.backpack)):
            if role.unit_id in self.p.engaged: continue
            if self.m.jobs.get(role.unit_id,{}).get('type')=='upgrade': continue
            routes=self.p.route(role)
            candidates=sorted(self.held_options(role),key=lambda o:
                              (o[0],routes.distance(o[4].pos),o[2]))
            for _,level,uid,item,target in candidates:
                if (uid not in occupied and routes.distance(target.pos)<INF
                        and self.m.blocked_deliveries.get((role.unit_id,uid),0)<=self.turn.round_no):
                    self.m.jobs[role.unit_id]={'type':'upgrade','unit':uid,'item':item,
                        'level':level,'bought':True,'price':0}
                    occupied.add(uid)
                    break
        if not self.turn.is_day or len(self.turn.weapons())<3: return
        # issue #3 的修复包储备见下方（排在升级券采购之后）
        # issue #3: 取消"首日金钱>100 优先买大机器人召唤令"的逻辑。
        # 前期金币优先用于武器升级 / 围墙升级 / 储备修复包；
        # 只有在武器升到2级以上、迎敌半圈围墙阶段完成、且进入第3天之后，
        # 才把富余金币用于机器人召唤令骚扰。
        if (self.m.day>=3 and not self.m.summon_order_done
                and all(max(1,min(3,w.level))>=2 for w in self.turn.weapons())
                and not self.wall_phase_active()
                and SUMMON_ORDER in self.p.shop_prices
                and self.p.gold>SUMMON_ORDER_TRIGGER
                and not any(j.get('type')=='order' for j in self.m.jobs.values())
                and not any(SUMMON_ORDER in r.backpack for r in self.turn.controllable())):
            courier=self._courier_for(self.p.shop_prices[SUMMON_ORDER])
            if courier:
                role,shop=courier
                self.m.jobs[role.unit_id]={'type':'order','item':SUMMON_ORDER,
                    'price':self.p.shop_prices[SUMMON_ORDER],'shop':shop}
                self.m.event(f'worker {role.unit_id}: buy {SUMMON_ORDER} after weapons/defense secured')
                return
        # One purchasing courier at a time; the other worker keeps producing money.
        if any(j.get('type') in ('upgrade','order') for j in self.m.jobs.values()): return
        shops=[q for q,k in self.turn.zones.items() if k=='weaponShop']
        # 为尚未满级的武器保留券预算，围墙券只使用超出的金币。
        weapon_reserve=0
        if not self.wall_voucher_first():      # 围墙券先行时不替武器券攒钱(否则墙券永远买不起)
            for w in self.turn.weapons():
                lv=max(1,min(3,w.level))
                if lv<3:
                    need='WeaponUpgradeVoucher%d'%lv
                    weapon_reserve=max(weapon_reserve,int(self.p.shop_prices.get(need,0)))
        for priority,level,uid,item,target in self.options():
            price=self.p.shop_prices.get(item)
            if price is None or price>self.p.gold: continue
            # 本次计划购买的数量(批量), 需在预算判断之前算出
            need=(self.wall_quota_left() if item.startswith('Wall')
                  else self.weapon_quota_left() if item.startswith('Weapon') else 1)
            held=sum(r.backpack.count(item) for r in self.turn.controllable())
            buy_n=max(1,min(max(1,need-held), int(self.p.gold//price)))
            if item.startswith('Wall') and weapon_reserve>0:
                # 按"本次实际花费"(单价*数量)判断, 否则批量购买会绕过武器券预算
                if self.p.gold-price*buy_n < weapon_reserve: continue
            # 第一天: 围墙是"用石头现场建"的，不得用采购额度抢武器升级的预算；
            # 武器升级券与保命项照常允许（需求3: 武器升级尽量在第一天完成）。
            day=self.m.day or ((self.turn.round_no-1)//130+1)
            if day==1 and self.p.missing_walls() and not item.startswith('Weapon') and not (item.startswith('Station') and self.station_urgent()):
                continue
            choices=[]
            for role in self.turn.workers():
                if role.backpack_full: continue
                if self.m.blocked_deliveries.get((role.unit_id,uid),0)>self.turn.round_no: continue
                count=self.purchase_count(role,item,price,buy_n,occupied)
                if count<1: continue
                routes=self.p.route(role)
                for shop in shops:
                    stand=routes.adjacent(shop)
                    if stand is None: continue
                    delivery=self.p.geo.to(target.pos).get(stand,INF)
                    return_cost=max((self.p.home_cost(role,s) for s in self.p.geo.seats(target.pos)),default=INF)
                    cost=routes.cost[stand]+delivery+return_cost+2+JOURNEY_MARGIN
                    if cost<self.p.remaining:
                        choices.append((cost,role,shop,count))
            if choices:
                _,role,shop,buy_n=min(choices,key=lambda x:x[0])
                # 批量采购: 同一种券一次买够(受当天配额与金币限制, 见上方 need/buy_n)
                self.m.jobs[role.unit_id]={'type':'upgrade','unit':uid,'item':item,
                    'level':level,'bought':False,'price':price,'shop':shop,'num':buy_n}
                self.m.event(f'worker {role.unit_id}: purchase {buy_n}x {item} for building {uid}')
                return
        # 修复包只在当天的围墙/武器升级配额都完成后, 用闲钱买
        if self.wall_quota_left()==0 and self.weapon_quota_left()==0 \
                and not any(j.get('type') in ('upgrade','order') for j in self.m.jobs.values()):
            stock=sum(r.backpack.count('WallFixer') for r in self.turn.workers())
            price=self.p.shop_prices.get('WallFixer')
            damaged=len(self.fixer_targets())
            target=min(WALL_FIXER_RESERVE, max(2, damaged)) if damaged else WALL_FIXER_RESERVE
            if (self.turn.walls() and price is not None and stock<target
                    and self.p.gold>price):
                courier=self._courier_for(price,skip_item='WallFixer')
                if courier:
                    role,shop=courier
                    buy_n=max(1,min(target-stock,int(self.p.gold//price)))
                    self.m.jobs[role.unit_id]={'type':'order','item':'WallFixer',
                        'price':price,'shop':shop,'keep':True,'num':buy_n}
                    self.m.event(f'worker {role.unit_id}: spare gold -> {buy_n}x WallFixer')
                    return

    def _nearest_shop(self,routes):
        shops=[q for q,k in self.turn.zones.items() if k=='weaponShop']
        return min(shops,key=routes.distance,default=None)

    def _courier_for(self,price,skip_item=None):
        """挑选能在回防期限前完成商店往返的工人。"""
        shops=[q for q,k in self.turn.zones.items() if k=='weaponShop']
        best=None
        for role in self.turn.workers():
            if role.backpack_full: continue
            if skip_item and skip_item in role.backpack: continue   # 别派给已持有该物品的人
            routes=self.p.route(role)
            for shop in shops:
                stand=routes.adjacent(shop)
                if stand is None: continue
                cost=routes.cost[stand]+self.p.home_cost(role,stand)+2+JOURNEY_MARGIN
                if cost<self.p.remaining and (best is None or cost<best[0]):
                    best=(cost,role,shop)
        return (best[1],best[2]) if best else None

    def order(self,role,routes):
        """召唤令采购/使用: 买到即用（使用不需要目标位置）。"""
        job=self.m.jobs.get(role.unit_id)
        if not job or job.get('type')!='order': return False
        item=job['item']
        if item in role.backpack:
            self.m.jobs.pop(role.unit_id,None)
            if job.get('keep'):
                self.m.event(f'worker {role.unit_id}: WallFixer stocked')
                return True          # 储备件: 留待修墙时使用
            self.p.commands[str(role.unit_id)]={'action':'use','name':item}
            self.m.summon_order_done=True
            self.m.event(f'worker {role.unit_id}: use {item}')
            return True
        if not self.turn.is_day or job['price']>self.p.gold:
            self.m.jobs.pop(role.unit_id,None)
            return False
        shop=job['shop']
        if routes.distance(shop)>=INF:
            self.m.jobs.pop(role.unit_id,None)
            return False
        num=max(1,int(job.get('num',1)))
        num=self.purchase_count(role,item,job['price'],num)
        if num<1:
            self.m.jobs.pop(role.unit_id,None)
            return False
        if self.p.interact(role,routes,shop,'buy',name=item,num=num):
            if routes.distance(shop)==0: self.p.gold-=job['price']*num
            return True
        return False

    def act_urgent(self,role,routes):
        """入夜前也必须完成的事: 召唤令采购/使用 + 升级券送达。"""
        return self.order(role,routes) or self.upgrade(role,routes)

    def spend_ready(self,role,routes):
        """是否到了"去商店把闲钱花掉再回家"的时点。

        判据: 手上至少有 10 金, 且剩余回合刚好够"去商店 + 买 + 回家"。
        这样不会因为回家触发太晚而白白把金币带回家。
        """
        if self.p.gold < SPEND_FIXER: return False
        shop=self._nearest_shop(routes)
        if shop is None: return False
        stand=routes.adjacent(shop)
        if stand is None: return False
        trip=routes.cost[stand]+1+self.p.home_cost(role,stand)
        return self.p.remaining <= trip+SPEND_TRIP_SLACK

    def spend_before_home(self,role,routes):
        """回家前尽量把闲钱花掉（升级券优先, 然后修复包）。

        规则(用户给定): 30 元 -> 一张围墙升级券 + 一个修复包;
        20 元 -> 一张围墙升级券; 10 元 -> 一个修复包。
        前提是这趟商店往返仍能在第 75 回合死线前回到武器旁。
        """
        if role.backpack_full: return False
        shop=self._nearest_shop(routes)
        if shop is None: return False
        gold=self.p.gold
        price_fixer=self.p.shop_prices.get('WallFixer')
        options=[]
        # 选一件"买得起、且最值钱"的东西: 优先级 武器券 > 围墙券 > 修复包
        for _,level,uid,item,target in self.options():
            price=self.p.shop_prices.get(item)
            if price is None or price>gold: continue
            rank=0 if item.startswith('Weapon') else 1 if item.startswith('Wall') else 2
            options.append((rank,-price,item,price))
        if price_fixer is not None and price_fixer<=gold and self.turn.walls():
            options.append((2,-price_fixer,'WallFixer',price_fixer))
        if not options:
            return False
        options.sort()
        # 武器升级券优先: 当天武器升级还没完成、而手上钱又买不起武器券时,
        # 不要把闲钱花在围墙券/修复包上(攒着, 尽快把武器升上去)
        needs_weapon=any(max(1,min(3,w.level))<3 for w in self.turn.weapons())
        if (needs_weapon and self.weapon_quota_left()>0
                and not any(o[2].startswith('Weapon') for o in options)):
            return False
        item,price=options[0][2],options[0][3]
        # 只在"去商店 + 买 + 回家"仍来得及的情况下绕路
        stand=routes.adjacent(shop)
        if stand is None: return False
        cost=routes.cost[stand]+1+self.p.home_cost(role,stand)+SPEND_TRIP_SLACK
        if cost>self.p.remaining and stand!=role.pos:
            return False
        if stand==role.pos:
            self.p.commands[str(role.unit_id)]={'action':'buy','name':item,'num':1}
            self.p.gold-=price
            self.m.event(f'worker {role.unit_id}: spend-before-home buy {item}')
            return True
        return self.p.move(role,routes,stand)

    def shop_standby(self,role,routes):
        """需求2: 开拓者没有任务可做时, 不要在基地闲着 —— 去商店旁待命按计划买券。

        前提(硬性): 必须能在**白天结束前**回到武器塔旁。所以这里的判据是
        "当前回合号 + 完整返程回合数 <= 白天最后一回合(70)", 而不是工人那套
        算到第 75 回合的死线 —— 否则开拓者会在傍晚还留在商店, 天黑了才往回走,
        夜里前 5-10 回合不在炮位上(实测 day1 到第 78 回合、day3 到第 80 回合才归位)。
        放不下这趟差事就交回 assign_towers, 由它把开拓者送回武器塔旁。
        """
        if not self.turn.is_day or role.backpack_full: return False
        shop=self._nearest_shop(routes)
        if shop is None: return False
        stand=routes.adjacent(shop)
        if stand is None: return False
        # 返程用"绕开待建围墙格"的精确距离 + 1 回合安全余量(实际寻路要绕那一圈)
        trip=routes.cost[stand]+1+self.p.home_cost(role,stand,avoid_parking=True)+1
        rod=(self.turn.round_no-1)%130+1
        if rod+trip>DAY_ROUNDS: return False          # 白天结束前回不到塔 -> 不站店, 立刻回家
        if distance(role.pos,shop)<=1 or stand==role.pos:
            # 已站到商店旁: 有券就买; 没得买也留在店里等钱/等任务(不回基地空转)
            self.buy_at_shop(role)
            return True
        return self.p.move(role,routes,stand)

    def buy_at_shop(self,role):
        """按既定购买计划买券: 计划内最高优先级且买得起的, 一次买够(批量采购)。

        计划顺序与工人采购共用 self.options(): 武器升级券优先 -> 围墙券 -> 基地券;
        计划内都买不起/已买够时, 用闲钱补修复包(与工人共享库存上限, 不会重复买)。
        与"回家清钱"同一条纪律: 武器还没满级且当前买不起武器券时, 不把钱花在
        围墙券/修复包上(攒着先升武器) —— 否则第一天的金币会被围墙券吃掉。
        """
        candidates=list(self.options())
        needs_weapon=any(max(1,min(3,w.level))<3 for w in self.turn.weapons())
        affordable_weapon=any(self.p.shop_prices.get(item,10**9)<=self.p.gold
                              for _,_,_,item,_ in candidates if item.startswith('Weapon'))
        if (needs_weapon and self.weapon_quota_left()>0 and not affordable_weapon
                and not self.wall_voucher_first()):
            return False        # 围墙券先行时改买围墙券
        for _,level,uid,item,target in candidates:
            price=self.p.shop_prices.get(item)
            if price is None or price<=0: continue
            need=(self.wall_quota_left() if item.startswith('Wall')
                  else self.weapon_quota_left() if item.startswith('Weapon') else 1)
            num=self.purchase_count(role,item,price,max(1,need))
            if num<1: continue
            self.p.commands[str(role.unit_id)]={'action':'buy','name':item,'num':num}
            self.p.gold-=price*num
            self.m.event(f'pioneer {role.unit_id}: standby purchase {num}x {item} for {uid}')
            return True
        price=self.p.shop_prices.get('WallFixer')
        if price is None or not self.turn.walls() or self.p.gold<price: return False
        if any(j.get('item')=='WallFixer' for j in self.m.jobs.values()): return False
        stock=sum(r.backpack.count('WallFixer') for r in self.turn.controllable())
        damaged=len(self.fixer_targets())
        if stock>=max(1, min(WALL_FIXER_RESERVE, damaged)): return False
        num=max(1,min(WALL_FIXER_RESERVE-stock,int(self.p.gold//price)))
        self.p.commands[str(role.unit_id)]={'action':'buy','name':'WallFixer','num':num}
        self.p.gold-=price*num
        self.m.event(f'pioneer {role.unit_id}: standby purchase {num}x WallFixer')
        return True

    def harvest_near_home(self,role,routes):
        """需求3: 白天回防途中在家附近顺手采矿。

        只做"不耽误入夜前回到武器旁"的顺路采集：矿点必须在基地附近，
        且 走到矿点 + 采一次 + 从矿点回位 + 返程余量 仍在白昼剩余回合内。
        夜间不走这里 —— 夜间开工(需求4)由 Economy.act 直接去次日矿点采矿。
        """
        if not self.turn.is_day: return False
        if role.backpack_full: return False
        station=self.turn.station()
        if station is None: return False
        best=None
        home_now=self.p.home_cost(role,role.pos)
        for mine,kind in self.turn.zones.items():
            price=self.p.prices.get(kind,0)
            if kind not in ORES or price<=0 or self.blocked(mine,kind): continue
            for seat in self.p.geo.seats(mine):
                travel=routes.cost.get(seat,INF)
                if travel>=INF: continue
                home=self.p.home_cost(role,seat)
                # "顺手"判据: 家附近 或 相对当前返程路线只多绕 HARVEST_DETOUR 回合内
                # （矿点不会生成在基地建造区内, 单看半径往往永远不中）
                detour=travel+1+home-home_now
                if distance(mine,station.pos)>NEAR_HOME_RADIUS and detour>HARVEST_DETOUR:
                    continue
                if travel+1+home+HARVEST_BUFFER<=self.p.remaining:
                    if best is None or travel<best[0]:
                        best=(travel,mine,seat)
        if best is None: return False
        travel,mine,seat=best
        if role.pos==seat:
            self.p.commands[str(role.unit_id)]={'action':'collect','targetPos':[mine.dump()]}
            self.m.event(f'worker {role.unit_id}: harvest {self.turn.zones[mine]} near home on the way back')
            return True
        return self.p.move(role,routes,seat)

    def upgrade(self,role,routes):
        job=self.m.jobs.get(role.unit_id)
        if not job or job.get('type')!='upgrade': return False
        target=self.target(job)
        if not self.usable(job,target):
            self.m.jobs.pop(role.unit_id,None)
            return False
        if job['item'] in role.backpack:
            if routes.distance(target.pos)==0:
                acted=self.p.interact(role,routes,target.pos,'use',name=job['item'],targetPos=[target.pos.dump()])
                self.m.event(f'worker {role.unit_id}: use {job["item"]} on {target.unit_id}')
                return acted
            if routes.distance(target.pos)>=INF:
                # 目标不可达(被围死等): 放弃本次差事, 免得整夜绕路
                self.defer_delivery(role,job)
                return False
            if self.turn.is_day:
                if self.p.enough_time(role,routes,target.pos):
                    return self.p.interact(role,routes,target.pos,'use',
                                           name=job['item'],targetPos=[target.pos.dump()])
                return False
            # 夜间: 必须先把手上的券用掉再去武器位（不允许揣着券过夜防御）。
            # 走到目标旁即用, 用完下回合由 assign_towers 送回武器旁。
            acted=self.p.interact(role,routes,target.pos,'use',
                            name=job['item'],targetPos=[target.pos.dump()])
            self.m.event(f'worker {role.unit_id}: night delivery of {job["item"]}')
            return acted
        if not self.turn.is_day: return False
        if self.p.remaining<=self.p.home_cost(role,role.pos)+5:
            self.m.jobs.pop(role.unit_id,None)
            return False
        shop=job['shop']
        if job['price']>self.p.gold or role.backpack_full or routes.distance(shop)>=INF:
            self.m.jobs.pop(role.unit_id,None)
            return False
        num=max(1,int(job.get('num',1)))
        num=self.purchase_count(role,job['item'],job['price'],num)
        if num<1:
            self.m.jobs.pop(role.unit_id,None)
            return False
        if self.p.interact(role,routes,shop,'buy',name=job['item'],num=num):
            if routes.distance(shop)==0: self.p.gold-=job['price']*num
            return True
        return False

    def next_day_mine(self,role,routes):
        """需求4: 夜间为下一天挑矿点, 返回应站到的采集邻格(方便次日开采)。

        评分沿用白天的"金币/回合"(含到小贩与回防的完整行程), 但预算按新的一天
        75 回合算, 避免夜间 remaining=0 导致一个候选都算不出来。
        """
        kinds=self.wanted_kinds(role)
        candidates=self.mining_candidates(role,routes,kinds,horizon=DAY_ROUNDS)
        if not candidates:
            candidates=self.mining_candidates(role,routes,horizon=DAY_ROUNDS)
        if not candidates: return None
        best=max(candidates,key=lambda c:(c['score'],-c['total']))
        # 注意: 这里不认领矿点 —— 夜间工人的行程统一由 act() 的 job 决定,
        # 若在此处认领, act() 会跳过该矿另选一个, 两套规划互相推翻(曾在矿周围打转)。
        return best['entry']

    def blocked(self,pos,kind):
        if self.m.mine_blocked_until.get(pos,0)>self.turn.round_no: return True
        return any(e['kind']==kind and e['start_day']<=self.m.day<=e['end_day']
                   for e in self.m.news_advice.get('blocked_mines',[]))

    # ---------------------------------------------------------------- 矿工分工
    def stone_needed(self):
        """是否还需要采石: 建墙缺格, 或手上石料不足以补齐。"""
        missing=len(self.p.missing_walls())
        if missing<=0: return False
        stock=sum(r.backpack.count('stone') for r in self.turn.workers())
        return stock<missing

    def family(self,role):
        """采石/采矿的时段统筹（用户要求: 石头早上采、夜晚采矿）:

        - 夜晚: 一律采矿石(铁/铜)赚钱 —— 夜里不采石;
        - 白天早上(STONE_MORNING_ROUNDS 之前): 需要石头时采石(半圈/加长墙要用的料);
        - 白天其余时间: 采矿石赚钱(采石只由建墙逻辑按缺料即时触发, 不再长期占人);
        - 第1天前 DAY1_ORE_PHASE_ROUNDS 回合仍先全员采铁/铜赚钱。
        """
        rod=(self.turn.round_no-1)%130+1        # 当天回合号(1..130)
        if not self.turn.is_day:
            return 'ore'                        # 夜晚采矿
        if self.m.day==1 and rod<=DAY1_ORE_PHASE_ROUNDS:
            self.m.mining_roles={}
            return 'ore'
        if rod>STONE_MORNING_ROUNDS:
            return 'ore'                        # 过了早上不再派人专门采石
        return 'stone' if self.stone_needed() else 'ore'

    def wanted_kinds(self,role):
        fam=self.family(role)
        if fam=='stone': return ('stone',)
        return ('iron','copper')          # 赚钱阶段优先铁/铜(单价高)

    @staticmethod
    def journey_score(value, sale_time, home_time):
        """采矿与立即出售都按金币 / 完整出售回防行程比较。"""
        return value / max(1, sale_time + home_time)

    def sale_candidates(self,role,routes,extra=None):
        # 需求5: 石头不卖 —— 只把小贩收购的矿石列入出售候选, 石头留给建墙/重建
        stock=Counter(x for x in role.backpack if x in SELLABLE)
        if extra: stock.update(extra)
        if not stock: return []
        value=sum(n*self.p.prices.get(k,0) for k,n in stock.items())
        candidates=[]
        for vendor,k in self.turn.zones.items():
            if k!='vendor': continue
            for seat in self.p.geo.seats(vendor):
                walk=routes.cost.get(seat,INF)
                home=self.p.home_cost(role,seat)
                total=walk+len(stock)+home+JOURNEY_MARGIN
                if total<self.p.remaining:
                    candidates.append({'score':self.journey_score(value,walk+len(stock),home),
                        'vendor':vendor,'seat':seat,'total':total,'stock':stock,'value':value})
        return candidates

    def mining_candidates(self,role,routes,kinds=None,horizon=None):
        """矿点候选。horizon 用于"夜间为下一天预置站位": 按新的一天预算评估,
        不受当天剩余回合(夜间为 0)限制。"""
        capacity=max(0,(role.capacity or 100)-len(role.backpack))
        if not capacity: return []
        remaining=self.p.remaining if horizon is None else horizon
        stock=Counter(x for x in role.backpack if x in SELLABLE)
        inventory_value=sum(n*self.p.prices.get(k,0) for k,n in stock.items())
        candidates=[]
        vendors=[q for q,k in self.turn.zones.items() if k=='vendor']
        for mine,kind in self.turn.zones.items():
            price=self.p.prices.get(kind,0)
            if kind not in ORES or price<=0 or self.blocked(mine,kind): continue
            if kinds is not None and kind not in kinds: continue
            # 枚举所有可完成的采集量；由收益和返程预算选择批量。
            left_in_mine=max(1,10-self.m.mine_used.get(mine,0)-self.mine_claims[mine])
            if self.mine_claims.get(mine,0)>0:
                continue        # 要求6: 两名工人不同时采同一个矿
            quantity=min(capacity,left_in_mine)
            sales=len(set(stock)|{kind})
            for entry in self.p.geo.seats(mine):
                approach=routes.cost.get(entry,INF)
                if approach>=INF: continue
                # Consider each actual vendor seat: obstacles and the final return
                # are included together, not independently minimized legs.
                for vendor in vendors:
                    for exit in self.p.geo.seats(vendor):
                        trip=self.p.geo.field([exit]).get(entry,INF)
                        home=self.p.home_cost(role,exit)
                        max_q=min(quantity,remaining-approach-trip-sales-home-JOURNEY_MARGIN-1)
                        if max_q<1: continue
                        for q in range(1,int(max_q)+1):
                            time=approach+q+trip+sales
                            score=self.journey_score(inventory_value+price*q,time,home)
                            candidates.append({'score':score,'target':mine,'kind':kind,
                                'entry':entry,'vendor':vendor,'exit':exit,'left':q,
                                'total':time+home+JOURNEY_MARGIN,'price':price,'type':'mine'})
        return candidates

    def sell(self,role,routes,job):
        stock=Counter(x for x in role.backpack if x in SELLABLE)
        if not stock:
            self.m.jobs.pop(role.unit_id,None)
            return False
        vendor=job['vendor']
        seat=job.get('exit',job.get('seat'))
        if seat not in routes.cost:
            seat=routes.adjacent(vendor)
        if seat is None: return False
        if self.p.remaining <= routes.cost[seat]+len(stock)+self.p.home_cost(role,seat)+1:
            return False
        if distance(role.pos,vendor)<=1:
            name=max(stock,key=lambda k:stock[k]*self.p.prices.get(k,0))
            self.p.commands[str(role.unit_id)]={'action':'sell','name':name,'num':stock[name]}
            return True
        return self.p.move(role,routes,seat)

    def act(self,role,routes):
        if self.order(role,routes): return True
        if self.upgrade(role,routes): return True
        job=self.m.jobs.get(role.unit_id)
        if job and job.get('type')=='sell':
            if self.sell(role,routes,job): return True
            self.m.jobs.pop(role.unit_id,None)
        if job and job.get('type')=='mine':
            invalid=(self.turn.zones.get(job['target'])!=job['kind'] or
                     self.blocked(job['target'],job['kind']) or role.backpack_full)
            changed_price=self.p.prices.get(job['kind'])!=job['price']
            if not invalid and not changed_price and job['left']>0:
                entry=job['entry']; exit=job['exit']
                cost=(routes.cost.get(entry,INF)+job['left']+
                      self.p.geo.field([exit]).get(entry,INF)+3+self.p.home_cost(role,exit)+JOURNEY_MARGIN)
                if cost<self.p.remaining:
                    self.mine_claims[job['target']]+=job['left']
                    if role.pos==entry:
                        self.p.commands[str(role.unit_id)]={'action':'collect','targetPos':[job['target'].dump()]}
                        return True
                    if self.p.move(role,routes,entry): return True
            if job['left']<=0 and not changed_price:
                sale={'type':'sell','vendor':job['vendor'],'exit':job['exit']}
                self.m.jobs[role.unit_id]=sale
                if self.sell(role,routes,sale): return True
            self.m.jobs.pop(role.unit_id,None)
        sales=self.sale_candidates(role,routes)
        # 分工采集: 采石工只去石矿, 其余工只去铁矿/铜矿; 本工种无可用矿时回退到全部矿种
        kinds=self.wanted_kinds(role)
        mines=self.mining_candidates(role,routes,kinds)
        if not mines:
            mines=self.mining_candidates(role,routes)
        sale=max(sales,key=lambda x:x['score'],default=None)
        mine=max(mines,key=lambda x:x['score'],default=None)
        value=sum(self.p.prices.get(k,0) for k in role.backpack if k in ORES)
        funding=next((self.p.shop_prices[item] for _,_,_,item,_ in self.options()
                      if item in self.p.shop_prices and self.p.shop_prices[item]>self.p.gold),None)
        unlock=funding is not None and self.p.gold+value>=funding
        # 批量原则: 手上的矿没攒够 MIN_SELL_BATCH 且背包未满时不专程跑小贩,
        # 除非正好差钱采购(funding)或已经顺路(vendor 就在旁边)。
        stock_now=sum(1 for x in role.backpack if x in ORES)
        near_vendor=any(distance(role.pos,v)<=1 for v in
                        [q for q,k in self.turn.zones.items() if k=='vendor'])
        if sale is not None and not role.backpack_full and not unlock and not near_vendor \
                and stock_now<MIN_SELL_BATCH:
            sale=None
        if sale and (mine is None or sale['score']>=mine['score'] or unlock):
            self.m.jobs[role.unit_id]={'type':'sell',**sale}
            return self.sell(role,routes,self.m.jobs[role.unit_id])
        if mine:
            self.m.jobs[role.unit_id]=mine
            self.mine_claims[mine['target']]+=mine['left']
            if role.pos==mine['entry']:
                self.p.commands[str(role.unit_id)]={'action':'collect','targetPos':[mine['target'].dump()]}
                return True
            return self.p.move(role,routes,mine['entry'])
        return False
