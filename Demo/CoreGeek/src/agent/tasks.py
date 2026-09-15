"""Generic task/LLM protocol, daily news reasoning, and treasure execution.

LLM-generated shell text is returned ONLY in executeCmd for the judge sandbox.
It is never executed by this agent's host process.

自进化任务采用"固定探测 -> 知识汇总 -> LLM 给 API 调用"的分阶段流程：
  1) 前 3 回合用固定命令探测任务目录（只探测 /tmp/selfEvolutionTask/，
     不做从根目录的全盘递归，避免 15 秒沙盒超时）；
  2) 把 3 轮探测结果汇总成一次信息完整的 prompt，让 LLM 直接给出正确 API 调用
     （带上赛事约定的 X-API-Key 头，并尝试不同参数组合）；
  3) 后续每轮把新的命令结果回灌给 LLM，逐步逼近正确答案。
"""
import hashlib
import json
from collections import Counter
from .protocol import Pos, distance
from .geography import INF

# 自进化任务目录与接口约定（赛事环境固定，禁止从 / 全盘递归）
TASK_DIR = '/tmp/selfEvolutionTask/'
HERITAGE_API_KEY = 'heritage-api-key-2024'
PROBES = (
    # 第1轮: 只看任务目录结构
    "find %s -maxdepth 3 2>/dev/null | head -80" % TASK_DIR,
    # 第2轮: 打印目录内文本文件内容（限深、限大小、带文件名分隔）
    ("find %s -maxdepth 3 -type f -size -64k 2>/dev/null | head -20 | "
     "while read f; do echo \"===== $f =====\"; cat \"$f\"; done | head -400" % TASK_DIR),
    # 第3轮: 抽取接口/参数线索
    ("grep -rInE 'http|api|key|token|param|curl|POST|GET|json' %s 2>/dev/null | head -60"
     % TASK_DIR),
)
PROBE_LIMIT = len(PROBES)


def task_param(text):
    """抽取任务参数(如"查询XX今天的天气"里的城市名), 用于复用时的命令参数替换。"""
    import re as _re
    m=_re.search(r'(?:查询|获取)([^\s,。，]{1,12}?)(?:今天|明天|当天|的天气)', str(text or ''))
    return m.group(1).strip() if m else ''

def parse_json(text):
    text = str(text or '').strip()
    if text.startswith('```'):
        text = text.split('\n',1)[-1].rsplit('```',1)[0].strip()
    try:
        value = json.loads(text)
        return value if isinstance(value,dict) else None
    except (ValueError, TypeError):
        return None


class Tasks:
    def __init__(self, planner):
        self.p = planner
        self.m = planner.memory
        self.turn = planner.turn
        self.payload = planner.payload
        self.pioneer = next(iter(self.turn.alive(('pioneer',))),None)

    def send(self, channel, prompt, token=''):
        if self.p.prompt or self.m.pending_llm:
            return False
        active = channel == 'task' and bool(self.payload.get('phaseTask'))
        if not active and self.m.llm_used >= 3:
            return False
        request_id = f'{self.turn.round_no}:{channel}:{token}'
        self.p.prompt = ('只返回JSON对象，不要Markdown。必须原样返回 request_id=' +
                         json.dumps(request_id) + '\n' + prompt)
        self.m.pending_llm = {'channel':channel,'token':token,'round':self.turn.round_no,
                              'request_id':request_id}
        if not active:
            self.m.llm_used += 1
        self.m.event(f'LLM {channel}; daily non-task usage {self.m.llm_used}/3')
        return True

    def receive(self):
        pending = self.m.pending_llm
        if not pending or pending['round'] >= self.turn.round_no:
            return
        self.m.pending_llm = None
        value = parse_json(self.payload.get('llmResp'))
        if not value or value.get('request_id') != pending['request_id']:
            self.m.event('LLM response missing/invalid/request_id mismatch; will retry within quota')
            return
        if pending['channel'] == 'task':
            if self.m.task and self.m.task['token'] == pending['token']:
                self.m.task['proposal'] = value
        else:
            self.accept_news(value)
            self.m.analysed_news = pending['token']

    def own_task_cells(self):
        """己方所有任务点格 —— 报价里查不到具体位置时的兜底交互范围。

        没有这个兜底时, "有 phaseTask 但 playerTasks 里查不到该点"的任务会以空
        positions 建档, 随后 solve 里 min([]) 抛 ValueError, 该回合所有角色的指令全部
        丢失(服务器兜底返回空指令, 表现为所有人原地不动、在外面的也不回来)。
        """
        prefix=self.turn.team_type + 'TaskPoint'
        return [p for p,k in self.turn.zones.items() if k.startswith(prefix)]

    def points(self):
        result = []
        prefix = self.turn.team_type + 'TaskPoint'
        own_zones = {p:k for p,k in self.turn.zones.items() if k.startswith(prefix)}
        for raw in self.payload.get('teamOur',{}).get('playerTasks',[]):
            try:
                pos = Pos.load(raw['taskPosition'])
            except (KeyError,ValueError,TypeError):
                continue
            if pos not in own_zones:
                continue
            positions = [q for q,k in own_zones.items() if k == own_zones[pos]]
            result.append({**raw,'positions':positions})
        return result

    def sync_task(self):
        desc = str(self.payload.get('phaseTask') or '')
        old = self.m.task
        if old and (not desc or desc != old['desc'] or self.pioneer is None):
            errors = {e.get('errorCode') for e in self.payload.get('errors',[])}
            last = self.m.last_commands.get(str(self.pioneer.unit_id),{}) if self.pioneer else {}
            result = (self.payload.get('lastRoundRoleActionResults') or {}).get(str(self.pioneer.unit_id)) if self.pioneer else False
            # A task disappearing alone also means timeout/death/abandonment.
            # Only a just-submitted, non-rejected answer earns a reusable SOP.
            success = (not desc and self.pioneer is not None and last.get('action') == 'submitAnswer'
                       and result is not False and not errors.intersection({1,2,4})
                       and self.turn.round_no <= old['start']+old['timeout']
                       and any(distance(self.pioneer.pos,q)<=1 for q in old['positions']))
            if success:
                # 提取本次成功用到的命令与被接受的答案, 供同类任务直接复用(省探测+LLM往返)
                last_cmd=next((h.get('command') for h in reversed(old['history']) if h.get('command')),None)
                last_answer=next((h.get('answer') for h in reversed(old['history']) if h.get('answer')),None)
                self.m.skills.append({'task':old['desc'][:4000], 'method':old.get('skill','')[:4000],
                                      'command':last_cmd, 'answer':last_answer,
                                      'param':task_param(old['desc']),
                                      'steps':old['history'][-6:]})
                self.m.skills[:] = self.m.skills[-12:]
                self.m.event('task completed; saved reusable procedure')
            self.m.task = None
            self.m.task_choice = None
        if desc and self.pioneer and self.m.task is None:
            choice = self.m.task_choice or min(self.points(),key=lambda x:min(
                distance(self.pioneer.pos,q) for q in x['positions']),default={})
            start = choice.get('accepted_round',self.turn.round_no-1)
            token = hashlib.sha256((desc+str(start)).encode()).hexdigest()[:12]
            positions=choice.get('positions') or self.own_task_cells()
            self.m.task = {'desc':desc,'start':start,'timeout':int(choice.get('timeoutRounds') or 100),
                           'token':token,'positions':positions,
                           'history':[],'proposal':None,'waiting_cmd':False,'skill':'',
                           'probes':0,'probe_results':[],'probing':False}
            # 同类任务快速通道: 题干结构一致(仅参数不同) -> 直接复用已验证的命令
            reusable=self.reusable(desc)
            if reusable:
                self.m.task['reuse']=reusable
                self.m.event('task matches a saved procedure; skip probing/LLM')

    def reusable(self,desc):
        """在已保存的 SOP 里找题干结构一致的条目(把数字/城市名等参数抽象掉再比)。"""
        import re as _re
        def norm(text):
            text=_re.sub(r'[0-9]+','#',str(text or ''))
            text=_re.sub(r'[\u4e00-\u9fa5]{2,4}(?=今天|的天气)','CITY',text)
            return text[:400].strip()
        key=norm(desc)
        for entry in reversed(self.m.skills):
            if entry.get('command') and norm(entry.get('task'))==key:
                return entry
        return None

    def can_work(self):
        """需求4: 夜间在「夜战结束」之后同样可以采矿/做任务(官方动作无昼夜限制)。"""
        return bool(self.turn.is_day or self.p.battle_over)

    def next_day_post(self,role):
        """需求4: 夜里先把开拓者送到次日任务点旁(只站位, 不领取任务)。"""
        if self.m.task or self.m.task_choice:
            return None                     # 已有进行中的任务 -> 不挪位
        routes = self.p.route(role)
        best = None
        for point in self.points():
            if not point.get('isValid') or point.get('coldDownRounds',0) > 0:
                continue
            target = min(point['positions'],key=routes.distance)
            stand = routes.adjacent(target)
            if stand is None:
                continue
            reward = float(point.get('scoreReward',0)) + float(point.get('goldReward',0))
            score = reward / max(1,routes.cost[stand])
            if best is None or score > best[0]:
                best = (score,stand)
        return best[1] if best else None

    def run(self):
        self.sync_task()
        self.receive()
        held = False
        if self.pioneer:
            if self.m.task:
                held = self.solve()
            elif self.turn.is_day and self.pioneer.unit_id not in self.p.engaged:
                held = self.treasure() or self.acquire()
            if held:
                self.p.engaged.add(self.pioneer.unit_id)
        # Actual task solving always has first claim on the single LLM channel.
        if not self.payload.get('phaseTask') and self.m.news:
            if self.m.analysed_news != self.m.news_version and self.m.news_attempts < 3:
                prompt = '''分析每日官方消息和累积民间传闻。不是自进化题，不执行命令。
返回 {"request_id":"原样回传", "blocked_mines":[{"kind":"stone/iron/copper","start_day":1,"end_day":2,"evidence":"原文依据"}],
"treasure":null 或 {"pos":{"x":0,"y":0},"start_round":1,"end_round":1300,"items":["商品英文名"],"confidence":0.95,"evidence":"原文依据"}}。
禁止编造地点/日期/物品；未能确定则treasure=null。天数转回合：第d天开始=(d-1)*130+1，夜晚开始=(d-1)*130+71。
地图原点左下，商品名称必须与清单完全一致，任务用品不是固定六种。blocked_mines仅填写明确停采时间，不把涨价等同停采。
'''+json.dumps({'news':self.m.news,'shop':self.payload.get('weaponShopList',[]),
               'width':self.turn.width,'height':self.turn.height},ensure_ascii=False)
                if self.send('news',prompt,self.m.news_version):
                    self.m.news_attempts += 1
        return held

    def acquire(self):
        routes = self.p.route(self.pioneer)
        candidates = []
        for point in self.points():
            if not point.get('isValid') or point.get('coldDownRounds',0)>0:
                continue
            target = min(point['positions'],key=routes.distance)
            # Unknown tasks get a useful initial budget, not the full timeout.
            # Different task families may need many turns; never assume weather.
            estimated = min(int(point.get('timeoutRounds') or 20),20)
            if not self.p.enough_time(self.pioneer,routes,target,estimated+1):
                continue
            reward = float(point.get('scoreReward',0))+float(point.get('goldReward',0))
            candidates.append((reward/(routes.distance(target)+estimated+1),point,target))
        if not candidates:
            self.m.task_choice = None
            return False
        _, point, target = max(candidates,key=lambda x:x[0])
        self.m.task_choice = dict(point)
        if routes.distance(target)==0:
            self.m.task_choice['accepted_round']=self.turn.round_no
        return self.p.interact(self.pioneer,routes,target,'acceptTask')

    def solve(self):
        task = self.m.task
        role = self.pioneer
        if not any(distance(role.pos,q)<=1 for q in task['positions']):
            # Do not issue executeCmd outside the known task interaction range;
            # 但任务仍有效就走回任务点(需求4: 开拓者去任务点做题),
            # 夜里战斗未结束时先守阵位。
            if not self.can_work() or not task['positions']:
                return False                      # 空 positions 时绝不 min([]) 抛异常
            routes = self.p.route(role)
            target = min(task['positions'],key=routes.distance)
            return self.p.move(role,routes,routes.adjacent(target))
        if task['waiting_cmd']:
            result = str(self.payload.get('lastCmdResult') or '')
            task['history'].append({'result':result[:40000] or '[missing command result]'})
            if task.get('probing'):
                task['probe_results'].append(result[:20000])
                task['probing'] = False
            task['waiting_cmd']=False
        if task.get('submitted'):
            task['history'].append({'feedback':self.payload.get('errors',[]),
                                    'task_still_active':True})
            task['submitted']=False
        proposal = task.pop('proposal',None)
        # Preserve the task while reasoning, but do not sacrifice mandatory defense.
        deadline = task['start']+task['timeout']
        returning = (not self.can_work()
                     or self.p.remaining <= self.p.home_cost(role,role.pos)+5)
        # 快速通道: 同类任务直接重放已验证命令(零探测、零 LLM 往返)
        if task.get('reuse') and not task['waiting_cmd'] and not returning:
            entry=task['reuse']
            if not task.get('reuse_done'):
                task['reuse_done']=True
                task['waiting_cmd']=True
                saved=entry.get('param') or ''
                now=task_param(task['desc'])
                command=entry['command']
                same=saved and now and saved==now
                if saved and now and not same:
                    # 参数不同: 把旧参数替换成新参数后重跑, 不能沿用旧结果/旧答案
                    command=command.replace(saved, now)
                task['reuse_same']=bool(same)
                task['history'].append({'command':command})
                self.p.execute_cmd=command
                self.m.event('task: replay saved procedure (%s)' % ('same params' if same else 'params substituted'))
                return True
            result=str(self.payload.get('lastCmdResult') or '')
            if not result:
                return False
            if task.get('reuse_same') and entry.get('answer'):
                # 完全同题: 直接提交已验证答案(零 LLM)
                answer=entry['answer']
                self.p.commands[str(role.unit_id)]={'action':'submitAnswer','taskAnswer':answer}
                task['history'].append({'answer':answer})
                task['submitted']=True
                task['reuse']=None
                self.m.event('task: submitted saved answer')
                return True
            # 参数已变: 用新结果请 LLM 给出答案(省掉 3 轮探测, 不做无依据作答)
            task['reuse']=None
            self.p.history_note=result[:20000]
            task['history'].append({'result':result[:20000]})
            prompt=('沙盒命令已返回结果, 请据此给出任务答案, 只返回JSON对象: '
                    '{"request_id":"原样回传","kind":"answer","answer":"答案","skill":"可复用方法"}。'
                    '禁止编造结果中不存在的信息。')
            self.send('task',prompt,task['token'])
            return True
        # 阶段一: 固定探测（只探测任务目录，最多 PROBE_LIMIT 轮，不消耗 LLM 额度）
        if task.get('probes',0) < PROBE_LIMIT and not task['waiting_cmd'] and not returning:
            command = PROBES[task['probes']]
            task['probes'] += 1
            task['probing'] = True
            task['waiting_cmd'] = True
            task['history'].append({'command':command})
            self.p.execute_cmd = command
            self.m.event(f'task probe {task["probes"]}/{PROBE_LIMIT}')
            return True
        if proposal:
            if isinstance(proposal.get('skill'),str):
                task['skill']=proposal['skill'][:4000]
            kind = proposal.get('kind')
            if kind=='answer' and 'answer' in proposal:
                answer=proposal['answer']
                answer=answer if isinstance(answer,str) else json.dumps(answer,ensure_ascii=False)
                self.p.commands[str(role.unit_id)]={'action':'submitAnswer','taskAnswer':answer}
                task['history'].append({'answer':answer[:12000]})
                task['submitted']=True
                return True
            command=proposal.get('command')
            if kind=='command' and isinstance(command,str) and command.strip() and len(command)<=16000 and not returning:
                self.p.execute_cmd=command
                task['history'].append({'command':command})
                task['waiting_cmd']=True
                return True
            task['history'].append({'feedback':'Invalid output schema or insufficient time for command.'})
        if returning or self.turn.round_no >= deadline:
            self.m.event('task time budget exhausted; pioneer returning to defend')
            return False
        self.send('task',self.knowledge_prompt(task,role),task['token'])
        return True

    def knowledge_prompt(self,task,role):
        """阶段二: 把 3 轮探测结果一次性喂给 LLM，要求直接给出可用的 API 调用。"""
        deadline=task['start']+task['timeout']
        return '''你是比赛开拓者的任务求解器，已完成沙盒任务目录的固定探测。现在直接给出可执行的 API 调用。
只返回一个JSON对象：
1. 需要调用接口：{"request_id":"原样回传","kind":"command","command":"一条shell命令","skill":"可复用方法"}
2. 信息已足够作答：{"request_id":"原样回传","kind":"answer","answer":"题目要求的答案","skill":"可复用方法"}
硬性要求：
- 命令用 curl 调用探测到的接口，必须带鉴权头 -H "X-API-Key: ''' + HERITAGE_API_KEY + '''"；
- 参数名与取值必须来自探测结果（文件内容/示例/字段名），不要臆造接口地址；
- 上一次命令失败或答案不完整时，换不同的参数组合再试（例如 id/序号/名称/日期逐个变化），不要重复同样的命令；
- 沙盒只允许基础 shell/python，无外网，单次最长 15 秒，输出上限 64KB；命令结果下一回合返回；
- 拿到关键输出后立刻用 kind=answer 提交，不要空转。
'''+json.dumps({'task':task['desc'],'round':self.turn.round_no,
                'deadline':min(deadline,self.turn.round_no+self.p.remaining-self.p.home_cost(role,role.pos)-5),
                'probe_results':task.get('probe_results',[])[-PROBE_LIMIT:],
                'history':task['history'][-10:],'known_procedures':self.m.skills[-6:],
                'errors':self.payload.get('errors',[])},ensure_ascii=False)

    def accept_news(self,value):
        text='\n'.join(str(entry.get(k,'')) for entry in self.m.news for k in ('officialNews','folkLegends'))
        blocked=[]
        for entry in value.get('blocked_mines',[]) if isinstance(value.get('blocked_mines',[]),list) else []:
            if not isinstance(entry,dict): continue
            start,end=entry.get('start_day'),entry.get('end_day')
            evidence=entry.get('evidence')
            if (entry.get('kind') in ('stone','iron','copper') and type(start) is int and type(end) is int
                    and 1<=start<=end<=10 and isinstance(evidence,str) and evidence and evidence in text):
                blocked.append(entry)
        self.m.news_advice={'blocked_mines':blocked}
        treasure=value.get('treasure')
        if not isinstance(treasure,dict): return
        try:
            raw=treasure['pos']
            if type(raw['x']) is not int or type(raw['y']) is not int: return
            pos=Pos.load(raw)
            start,end=treasure['start_round'],treasure['end_round']
            items=treasure['items']; evidence=treasure['evidence']
            shop={x['name'] for x in self.payload.get('weaponShopList',[])}
            if (0<=pos.x<self.turn.width and 0<=pos.y<self.turn.height
                and type(start) is int and type(end) is int and 1<=start<=end<=1300
                and isinstance(items,list) and items and all(isinstance(i,str) and i in shop for i in items)
                and isinstance(evidence,str) and evidence and evidence in text
                and float(treasure.get('confidence',0))>=0.9):
                self.m.treasure={**treasure,'pos':pos}
        except (KeyError,TypeError,ValueError):
            return

    def treasure(self):
        t=self.m.treasure
        if not t or self.m.treasure_done: return False
        previous=self.m.last_commands.get(str(self.pioneer.unit_id),{})
        if previous.get('action')=='summonTreasure':
            result=self.payload.get('lastSummonTreasureResult',0)
            if result in (1,4):
                self.m.treasure_done=True
                return False
        signature=(t['pos'],t['start_round'],t['end_round'],tuple(sorted(t['items'])))
        if signature in self.m.treasure_attempts or self.turn.round_no>t['end_round']:
            return False
        role=self.pioneer; routes=self.p.route(role)
        if not self.p.enough_time(role,routes,t['pos']): return False
        needed=Counter(t['items'])-Counter(role.backpack)
        if needed:
            shops=[q for q,k in self.turn.zones.items() if k=='weaponShop']
            shop=min(shops,key=routes.distance,default=None)
            if shop is None or role.capacity is None or len(role.backpack)+sum(needed.values())>role.capacity:
                return False
            reserve=25*max(0,3-len(self.turn.weapons()))
            prices=self.p.shop_prices
            if any(i not in prices for i in needed) or sum(prices[i]*n for i,n in needed.items())>self.p.gold-reserve:
                return False
            # Full shop -> altar -> home journey, not just a trip to the shop.
            if not self.p.enough_time(role,routes,shop,sum(needed.values())+self.p.geo.to(t['pos']).get(
                    routes.adjacent(shop),INF)+2): return False
            name=next(iter(needed))
            if self.p.interact(role,routes,shop,'buy',name=name,num=needed[name]):
                self.p.gold-=prices[name]*needed[name]
                return True
        arrival=self.turn.round_no+routes.distance(t['pos'])
        if arrival<t['start_round']-3: return False
        if routes.distance(t['pos'])>0:
            return self.p.move(role,routes,routes.adjacent(t['pos']))
        if self.turn.round_no<t['start_round']:
            return True
        self.p.commands[str(role.unit_id)]={'action':'summonTreasure','targetPos':[t['pos'].dump()],'item':t['items']}
        # A legal failure consumes the items too; no blind repeat attempts.
        self.m.treasure_attempts.add(signature)
        return True
