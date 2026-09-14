"""Task context, deterministic solvers, reusable guidance and LLM fallback.

Commands are returned to the judge; never executed by the agent process.
"""
import hashlib
import json
import logging
from collections import Counter
from .protocol import Pos, distance
from .geography import INF
from . import templates


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
            elapsed = max(1, self.turn.round_no-old['start'])
            self.m.task_stats['success' if success else 'failed'] += 1
            self.m.task_stats['rounds_total'] += elapsed
            self.event('TASK_SUCCESS' if success else 'TASK_FAIL', rounds=elapsed)
            self.m.task_runs.append({'family':old.get('signature',{}).get('family','unknown'),
                'success':success, 'rounds':elapsed, 'commands':old.get('commands',0),
                'llm_calls':old.get('llm_calls',0), 'source':old.get('source','unknown')})
            self.m.task_runs[:] = self.m.task_runs[-12:]
            if success:
                sig = old.get('signature') or templates.signature(old['desc'],old.get('context','')).dump()
                entry = {'family':sig['family'], 'signature':sig}
                if sig['family'] == 'unknown':
                    # Reuse methods as LLM guidance, never stale commands/answers.
                    entry.update(task=old['desc'][:4000], method=old.get('skill','')[:4000])
                self.m.skills.append(entry)
                self.m.skills[:] = self.m.skills[-12:]
            if self.m.pending_llm and self.m.pending_llm['channel'] == 'task':
                self.m.pending_llm = None
            self.m.task = None
            self.m.task_choice = None
        if desc and self.pioneer and self.m.task is None:
            choice = self.m.task_choice or min(self.points(),key=lambda x:min(
                distance(self.pioneer.pos,q) for q in x['positions']),default={})
            start = choice.get('accepted_round',self.turn.round_no-1)
            token = hashlib.sha256((desc+str(start)).encode()).hexdigest()[:12]
            self.m.task = {'desc':desc,'start':start,'timeout':int(choice.get('timeoutRounds') or 100),
                           'token':token,'positions':choice.get('positions',[]),
                           'history':[],'proposal':None,'waiting_cmd':False,'skill':'',
                           'context':'', 'context_done':False, 'result':'',
                           'result_seq':0, 'derived_seq':0, 'attempted':set(),
                           'commands':0, 'llm_calls':0, 'rejected':set()}
            self.m.task_stats['accepted'] += 1
            self.event('TASK_NEW')

    def event(self, name, **fields):
        # Only internal labels/counters; never credentials, task text or answers.
        text = name + ''.join(f' {key}={value}' for key,value in fields.items())
        self.m.event(text)
        logging.getLogger(__name__).info(text)

    def reusable(self, desc, context=''):
        sig = templates.signature(desc, context).dump()
        return next((entry for entry in reversed(self.m.skills)
                     if entry.get('signature') == sig), None)

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
            estimated = min(int(point.get('timeoutRounds') or 20),
                            templates.estimate_task_rounds(str(point.get('description') or '')))
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

    def dump(self, task):
        return task.get('context', '')

    def execute(self, task, command, source):
        digest = hashlib.sha256(command.encode()).hexdigest()
        if digest in task['attempted']:
            task['history'].append({'feedback':'Identical command already attempted; revise the procedure.'})
            return False
        task['attempted'].add(digest)
        task['waiting_cmd'] = True
        task['source'] = source
        task['commands'] += 1
        task['history'].append({'command':command, 'source':source})
        self.p.execute_cmd = templates.command('llm', script=command) if source == 'llm' else command
        self.event('TASK_EXEC', source=source)
        return True

    def submit(self, task, answer, source):
        answer = answer if isinstance(answer,str) else json.dumps(answer,ensure_ascii=False)
        if answer in task['rejected'] or len(answer.encode('utf-8')) > 12000:
            return False
        self.p.commands[str(self.pioneer.unit_id)] = {'action':'submitAnswer','taskAnswer':answer}
        task['history'].append({'answer':answer})
        task['submitted'] = answer
        self.event('TASK_SUBMIT', source=source, round_cost=self.turn.round_no-task['start'])
        return True

    def solve(self):
        task, role = self.m.task, self.pioneer
        if not any(distance(role.pos,q)<=1 for q in task['positions']):
            return False
        deadline = task['start']+task['timeout']
        if self.turn.round_no >= deadline:
            return False
        if task['waiting_cmd']:
            task['result'] = str(self.payload.get('lastCmdResult') or '')[:50000]
            task['result_seq'] += 1
            task['history'].append({'result':task['result'] or '[missing command result]'})
            task['waiting_cmd'] = False
            if task['source'] == 'context':
                task['context'] = task['result']
                task['context_done'] = True
                self.event('TASK_CONTEXT')
        if task.get('submitted'):
            task['rejected'].add(task['submitted'])
            task['history'].append({'feedback':self.payload.get('errors',[]), 'task_still_active':True})
            task['submitted'] = None
        proposal = task.pop('proposal',None)
        # A fresh result from ANY source gets first claim on this turn. Never
        # derive from an old result again after an answer rejection.
        if task['result_seq'] > task['derived_seq']:
            task['derived_seq'] = task['result_seq']
            answer = templates.derive_answer(task['desc'],self.dump(task),task['result'])
            if answer is not None and self.submit(task,answer,'direct'):
                self.event('TASK_DIRECT_ANSWER', source=task['source'])
                return True
        returning = not self.turn.is_day or self.p.remaining <= self.p.home_cost(role,role.pos)+5
        # Preserve an already available LLM answer even at the return boundary.
        if proposal:
            if isinstance(proposal.get('skill'),str):
                task['skill'] = proposal['skill'][:4000]
            if proposal.get('kind') == 'answer' and 'answer' in proposal:
                if self.submit(task,proposal['answer'],'llm'):
                    return True
            elif (proposal.get('kind') == 'command' and isinstance(proposal.get('command'),str)
                  and proposal['command'].strip() and len(proposal['command']) <= 16000 and not returning):
                if self.execute(task,proposal['command'],'llm'):
                    return True
            task['history'].append({'feedback':'Invalid, duplicate or late proposal; revise using current evidence.'})
        if returning:
            return False
        # Full descriptions can solve immediately; file-only tasks collect once.
        preset = templates.plan(task['desc'],self.dump(task)) if not task.get('template_tried') else None
        if preset:
            family, command = preset
            task['signature'] = templates.signature(task['desc'],self.dump(task)).dump()
            self.event('TASK_SIGNATURE', family=family)
            if self.reusable(task['desc'],self.dump(task)):
                self.m.task_stats['skill_hit'] += 1
                self.event('TASK_SKILL_MATCH', family=family)
            task['template_tried'] = True
            self.m.task_stats['template_hit'] += 1
            self.event('TASK_TEMPLATE_MATCH', family=family)
            return self.execute(task,command,'template')
        if not task['context_done'] and not task.get('template_tried'):
            return self.execute(task,templates.locate_command(task['desc']),'context')
        task['signature'] = templates.signature(task['desc'],self.dump(task)).dump()
        if self.send('task',self.knowledge_prompt(task,role),task['token']):
            task['llm_calls'] += 1
            self.m.task_stats['llm_calls'] += 1
            self.m.task_stats['llm_fallback'] += 1
            self.event('TASK_LLM_FALLBACK')
        return True

    def knowledge_prompt(self,task,role):
        deadline=task['start']+task['timeout']
        guidance = self.reusable(task['desc'],self.dump(task))
        return '''你是比赛开拓者的通用任务求解器。根据当前任务、文档、最新命令结果和错误反馈求解。
只返回一个JSON对象：
1. 需要执行命令：{"request_id":"原样回传","kind":"command","command":"一条shell命令","skill":"可复用方法"}
2. 信息足够作答：{"request_id":"原样回传","kind":"answer","answer":"题目要求的答案","skill":"可复用方法"}
硬性要求：
- 任务、URL、参数、鉴权和答案格式以本次文档为准；禁止复制旧题答案或臆造数据。
- 不局限于 API；可使用 shell/python 处理文件、工程修复或其他任务。
- 每条命令最多 15 秒，输出必须少于 48KB。自行设置网络/子进程超时，禁止从 / 全盘递归。
- 把相关步骤合成一条有界命令；API 数据完整性不确定时继续验证，不能用不完整的 sample 计算全量答案。
- 确定最终答案时可输出 __ANSWER <JSON/string>，checker 的 TOKEN 也会被直接识别。
- 模板或此前命令失败后必须修订方案，不要重复相同命令；新结果为空不能假装旧结果仍有效。
'''+json.dumps({'task':task['desc'],'round':self.turn.round_no,
                'deadline':min(deadline,self.turn.round_no+self.p.remaining-self.p.home_cost(role,role.pos)-5),
                'context':self.dump(task), 'latest_result':task['result'],
                'history':task['history'][-6:], 'known_procedure':guidance,
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
