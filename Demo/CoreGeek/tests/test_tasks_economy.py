import json
import unittest
from copy import deepcopy
from test_strategy import fixture, unit, apply_construction
from agent.brain import Planner, decide_response
from agent.memory import Memory
from agent.runtime import Agent
from agent.protocol import Turn,Pos,distance
from agent.tasks import Tasks


def task_fixture():
    p=fixture()
    p['teamOur']['roles']=[p['teamOur']['roles'][0],unit(4,'pioneer',12,25)]
    p['mapInfo']['zones']=[{'pos':{'x':13,'y':26},'neutralType':'challengerTaskPoint1'},
                           {'pos':{'x':20,'y':14},'neutralType':'defenderTaskPoint1'}]
    p['teamOur']['playerTasks']=[{'taskPosition':{'x':13,'y':26},'isValid':True,
        'coldDownRounds':0,'timeoutRounds':100,'scoreReward':50,'goldReward':30,'taskType':'自进化类1'}]
    return p


def advance_probes(p, m, start=2, limit=8):
    """推进固定探测阶段，直到本回合开始请求 LLM；返回 (该回合号, 该回合响应)。

    探测期只发射固定命令、不消耗 LLM 额度、也不发 prompt。
    """
    n = start
    for _ in range(limit):
        p['roundNo'] = n
        p['llmResp'] = ''
        r = decide_response(p, m)
        if r['prompt']:
            return n, r
        assert r['executeCmd'], f'探测阶段第{n}回合既无探测命令也无LLM请求: {r}'
        p['lastCmdResult'] = '[exitCode:0]\nprobe output at round %d' % n
        n += 1
    raise AssertionError('探测阶段未在限定回合内结束')


def llm_reply(memory,**fields):
    return json.dumps({'request_id':memory.pending_llm['request_id'],**fields},ensure_ascii=False)


def developed():
    p=fixture();p['roundNo']=131;p['teamOur']['goldNum']=250
    p['teamOur']['roles'] += [unit(10,'rocket',9,23),unit(11,'rocket',12,23),unit(12,'railgun',11,25)]
    p['mapInfo']['zones']=[{'pos':{'x':7,'y':24},'neutralType':'weaponShop'},
                           {'pos':{'x':6,'y':24},'neutralType':'vendor'},
                           {'pos':{'x':5,'y':24},'neutralType':'copper'}]
    p['weaponShopList']=[{'name':n,'price':v} for n,v in [
        ('WeaponUpgradeVoucher1',100),('WeaponUpgradeVoucher2',150),
        ('WallUpgradeVoucher1',20),('WallUpgradeVoucher2',30),('WallFixer',10)]]
    return p


class TaskTests(unittest.TestCase):
    def test_generic_task_command_feedback_answer_and_skill(self):
        for desc, command, output, answer in [
            ('读取 /data/sales.csv 并给出总销售额','python sum_sales.py','[exitCode:0]\n42',{'total':42}),
            ('求图中节点间最短路径，输入在 graph.json','python graph_solver.py','[exitCode:0]\nA C D',{'path':['A','C','D']}),
            ('按文档调用沙盒库存 API 查询商品余量','curl http://localhost:8000/stock','[exitCode:0]\n7',{'stock':7})]:
            with self.subTest(desc=desc):
                p=task_fixture();m=Memory()
                r=decide_response(p,m)
                self.assertEqual(r['roleCommandMap']['4']['action'],'acceptTask')
                self.assertFalse(r['executeCmd'])
                p['roundNo']=2;p['phaseTask']=desc
                n,r=advance_probes(p,m,2)
                self.assertIn(desc,r['prompt'])
                self.assertNotIn('4',r['roleCommandMap'])
                self.assertEqual(m.llm_used,0)
                p['roundNo']=n+1;p['lastCmdResult']=''
                p['llmResp']=llm_reply(m,kind='command',command=command,skill='复用输入解析与求解方法')
                r=decide_response(p,m)
                self.assertEqual(r['executeCmd'],command)
                self.assertFalse(r['prompt'])
                p['roundNo']=n+2;p['llmResp']='';p['lastCmdResult']=output
                r=decide_response(p,m)
                self.assertIn(output.split('\n')[-1],r['prompt'])
                p['roundNo']=n+3;p['llmResp']=llm_reply(m,kind='answer',answer=answer)
                r=decide_response(p,m)
                self.assertEqual(json.loads(r['roleCommandMap']['4']['taskAnswer']),answer)
                self.assertFalse(r['prompt'])
                p['roundNo']=n+4;p['phaseTask']='';p['llmResp']=''
                p['teamOur']['playerTasks'][0]['isValid']=False
                decide_response(p,m)
                self.assertEqual(len(m.skills),1)
                self.assertIsNone(m.task)

    def test_task_calls_unlimited_without_consuming_daily_quota(self):
        p=task_fixture();p['phaseTask']='任意复杂任务';m=Memory(day=1,llm_used=3)
        n,r=advance_probes(p,m,1)
        self.assertTrue(r['prompt'])
        for k in range(1,5):
            p['roundNo']=n+k;p['llmResp']='invalid'
            response=decide_response(p,m)
            self.assertTrue(response['prompt'])
            self.assertEqual(m.llm_used,3)
            self.assertNotIn('4',response['roleCommandMap'])

    def test_news_quota_three_and_resets_next_day(self):
        p=fixture();p['worldNews']={'officialNews':'今天矿价变化','folkLegends':''};m=Memory()
        calls=0
        for n in range(1,8):
            p['roundNo']=n;p['llmResp']='bad JSON'
            calls+=bool(decide_response(p,m)['prompt'])
        self.assertEqual(calls,3);self.assertEqual(m.llm_used,3)
        p['roundNo']=131
        self.assertTrue(decide_response(p,m)['prompt'])
        self.assertEqual(m.llm_used,1)

    def test_quota_error_stops_non_task_requests(self):
        p=fixture();p['worldNews']={'officialNews':'消息'}
        p['errors']=[{'errorCode':5}];m=Memory()
        self.assertFalse(decide_response(p,m)['prompt'])
        self.assertEqual(m.llm_used,3)

    def test_wrong_request_id_cannot_execute_command(self):
        p=task_fixture();p['phaseTask']='读取文件';m=Memory()
        decide_response(p,m)
        n,_=advance_probes(p,m,2)
        p['roundNo']=n+1;p['llmResp']=json.dumps({'request_id':'old','kind':'command','command':'bad'})
        r=decide_response(p,m)
        self.assertNotEqual(r['executeCmd'],'bad')
        self.assertTrue(r['prompt'])

    def test_partial_answer_feedback_does_not_create_skill(self):
        p=task_fixture();p['phaseTask']='多字段任务';m=Memory();decide_response(p,m)
        n,_=advance_probes(p,m,2)
        p['roundNo']=n+1;p['llmResp']=llm_reply(m,kind='answer',answer={'a':1})
        decide_response(p,m)
        p['roundNo']=n+2;p['llmResp']='';p['errors']=[{'errorCode':2,'description':'b字段缺失'}]
        r=decide_response(p,m)
        self.assertIn('b字段缺失',r['prompt']);self.assertFalse(m.skills)
        p['roundNo']=n+3;p['phaseTask']='';p['errors']=[{'errorCode':1}]
        decide_response(p,m);self.assertFalse(m.skills)

    def test_cannot_accept_enemy_or_cooling_point(self):
        p=task_fixture();p['teamOur']['playerTasks'][0]['coldDownRounds']=12
        p['teamOur']['playerTasks'].append({'taskPosition':{'x':20,'y':14},'isValid':True})
        self.assertNotIn('acceptTask',[x['action'] for x in decide_response(p)['roleCommandMap'].values()])

    def test_duplicate_round_idempotence_and_new_game_reset(self):
        p=fixture();p['worldNews']={'officialNews':'消息'};agent=Agent()
        r=agent.respond(p);self.assertEqual(r,agent.respond(p))
        memory=next(iter(agent.sessions.values()));self.assertEqual(memory.llm_used,1)
        p['roundNo']=4;agent.respond(p)
        p['roundNo']=1;agent.respond(p)
        self.assertIsNot(memory,next(iter(agent.sessions.values())))

    def test_news_result_is_not_task_answer(self):
        p=task_fixture();p['worldNews']={'officialNews':'消息'};m=Memory()
        decide_response(p,m)                        # 第1轮: 新闻 prompt(其 request_id 属于新闻通道)
        reply=llm_reply(m,kind='answer',answer='NOT A TASK ANSWER',blocked_mines=[])
        p['roundNo']=2;p['phaseTask']='新任务';p['llmResp']=reply
        r=decide_response(p,m)                      # 新闻回复到达时任务已开始
        self.assertNotIn('submitAnswer',[x['action'] for x in r['roleCommandMap'].values()])
        self.assertTrue(r['prompt'] or r['executeCmd'])

    def test_treasure_validated_items_and_single_attempt(self):
        p=task_fixture();p['teamOur']['playerTasks']=[]
        p['worldNews']={'folkLegends':'祭坛在(13,25)，第一天用星沙开启。'}
        p['weaponShopList']=[{'name':'StarSand','price':15}]
        p['teamOur']['roles'][1]['backpack']=['StarSand'];m=Memory()
        decide_response(p,m)
        p['roundNo']=2;p['llmResp']=llm_reply(m,treasure={'pos':{'x':13,'y':25},
            'start_round':1,'end_round':70,'items':['StarSand'],'confidence':0.99,
            'evidence':'祭坛在(13,25)，第一天用星沙开启。'})
        r=decide_response(p,m);self.assertEqual(r['roleCommandMap']['4']['action'],'summonTreasure')
        p['roundNo']=3;p['llmResp']='';p['lastSummonTreasureResult']=3
        r=decide_response(p,m)
        self.assertNotIn('summonTreasure',[x['action'] for x in r['roleCommandMap'].values()])


class EconomyTests(unittest.TestCase):
    def test_procurement_stays_committed_and_confirms_use(self):
        p=developed();m=Memory();upgraded=False;actions=[];per_role={}
        for n in range(131,155):
            p['roundNo']=n;r=decide_response(p,m)
            roles={str(u['id']):u for u in p['teamOur']['roles']}
            for uid,c in r['roleCommandMap'].items():
                if c['action']=='move': roles[uid]['pos']=c['targetPos'][0]
                elif c['action']=='buy':
                    actions.append('buy');per_role.setdefault(uid,[]).append('buy')
                    roles[uid]['backpack'].append(c['name'])
                    p['teamOur']['goldNum']-=next(x['price'] for x in p['weaponShopList'] if x['name']==c['name'])
                elif c['action']=='use':
                    actions.append('use');per_role.setdefault(uid,[]).append('use')
                    roles[uid]['backpack'].remove(c['name'])
                    target=next(u for u in p['teamOur']['roles'] if u['pos']==c['targetPos'][0])
                    target['level']+=1;upgraded=True
            if upgraded: break
        self.assertTrue(upgraded)
        # 采购一旦承诺就坚持送达并确认使用: 下单的那个角色必须完成 buy -> use
        # (开拓者也会在商店旁待命并按计划买券, 所以同期可能有多笔并行采购)
        couriers=[uid for uid,seq in per_role.items() if 'buy' in seq]
        self.assertTrue(couriers,per_role)
        self.assertEqual(per_role[couriers[0]][:2],['buy','use'],per_role)
        self.assertTrue(any('use' in seq for seq in per_role.values()),per_role)

    def test_wall_upgrade_candidates_and_dusk_use(self):
        p=developed();p['roundNo']=200
        p['teamOur']['roles'].append(unit(20,'wall',13,24,health=300))
        p['teamOur']['roles'][2]['backpack']=['WallUpgradeVoucher1']
        m=Memory();r=decide_response(p,m)
        self.assertEqual(r['roleCommandMap']['3']['action'],'use')
        self.assertEqual(r['roleCommandMap']['3']['name'],'WallUpgradeVoucher1')

    def test_level_three_wall_repair(self):
        p=developed();p['teamOur']['roles'].append(unit(20,'wall',13,24,health=300,level=3))
        p['teamOur']['roles'][2]['backpack']=['WallFixer']
        r=decide_response(p)
        self.assertEqual(r['roleCommandMap']['3']['name'],'WallFixer')

    def test_miner_route_uses_actual_vendor_detour(self):
        p=fixture();p['roundNo']=131
        p['teamOur']['roles']=[unit(1,'station',2,4),unit(2,'worker',10,15)]
        p['mapInfo']['zones']=[{'pos':{'x':12,'y':15},'neutralType':'copper'},
            {'pos':{'x':8,'y':15},'neutralType':'iron'}, {'pos':{'x':5,'y':15},'neutralType':'vendor'}]
        # A wall barrier makes the apparently nearby copper expensive to sell.
        p['teamEnemy']['roles']=[unit(100+y,'wall',11,y) for y in range(2,30)]
        planner=Planner(Turn.load(p),p);role=planner.turn.workers()[0];routes=planner.route(role)
        candidates=planner.economic.mining_candidates(role,routes)
        self.assertTrue(candidates)
        best=max(candidates,key=lambda x:x['score'])
        self.assertEqual(best['kind'],'iron')
        self.assertLess(best['total'],planner.remaining)

    def test_current_price_changes_mining_choice(self):
        p=fixture();p['roundNo']=131
        p['teamOur']['roles']=[unit(1,'station',2,4),unit(2,'worker',10,15)]
        p['mapInfo']['zones']=[{'pos':{'x':12,'y':15},'neutralType':'copper'},
            {'pos':{'x':8,'y':15},'neutralType':'iron'}, {'pos':{'x':10,'y':12},'neutralType':'vendor'}]
        p['vendorShopList']=[{'name':'copper','price':1},{'name':'iron','price':20}]
        planner=Planner(Turn.load(p),p);role=planner.turn.workers()[0]
        self.assertEqual(max(planner.economic.mining_candidates(role,planner.route(role)),key=lambda x:x['score'])['kind'],'iron')
        p['vendorShopList']=[{'name':'copper','price':20},{'name':'iron','price':1}]
        planner=Planner(Turn.load(p),p);role=planner.turn.workers()[0]
        self.assertEqual(max(planner.economic.mining_candidates(role,planner.route(role)),key=lambda x:x['score'])['kind'],'copper')

    def test_failed_collection_temporarily_blocks_mine(self):
        p=fixture();p['roundNo']=131;p['teamOur']['goldNum']=0
        p['mapInfo']['zones']=[{'pos':{'x':8,'y':24},'neutralType':'stone'}]
        m=Memory();r=decide_response(p,m)
        self.assertTrue(any(c['action']=='collect' for c in r['roleCommandMap'].values()))
        p['roundNo']=132;p['lastRoundRoleActionResults']={'2':False}
        decide_response(p,m)
        self.assertGreater(m.mine_blocked_until[Pos(8,24)],132)

    def test_confirmed_news_stops_mine_only_in_interval(self):
        p=fixture();p['worldNews']={'officialNews':'铁矿明天后天停工'};m=Memory()
        decide_response(p,m)
        p['roundNo']=2;p['llmResp']=llm_reply(m,blocked_mines=[{'kind':'iron','start_day':2,'end_day':3,'evidence':'铁矿明天后天停工'}])
        decide_response(p,m)
        planner=Planner(Turn.load(p),p,m)
        self.assertFalse(planner.economic.blocked(Pos(1,1),'iron'))
        m.day=2;self.assertTrue(planner.economic.blocked(Pos(1,1),'iron'))
        m.day=4;self.assertFalse(planner.economic.blocked(Pos(1,1),'iron'))


if __name__=='__main__': unittest.main()
