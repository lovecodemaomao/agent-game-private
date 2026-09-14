"""Real file-only tasks, executing generated commands against local fixtures."""
import contextlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from agent import templates
from agent.brain import decide_response
from agent.memory import Memory
from agent.task_sandbox import fingerprint
from test_tasks_economy import task_fixture, llm_reply
from test_strategy import unit


def execute(command):
    # Linux runs the exact shell command handed to the judge. Windows exercises
    # the identical encoded Python program without relying on a shell install.
    argv = ['sh', '-c', command] if os.name != 'nt' else [sys.executable, *shlex.split(command)[1:]]
    result = subprocess.run(argv, capture_output=True, timeout=14)
    if result.returncode:
        raise AssertionError('Fixture command failed: ' + result.stderr.decode('utf-8', 'replace')[-500:])
    return result.stdout.decode('utf-8')


def engineering(root, label, number, json_token=False, fail=False):
    workspace = root / ('ws_' + str(number))
    (workspace / 'config').mkdir(parents=True)
    (workspace / 'bin').mkdir()
    config = 'config/' + label + '.conf'
    (workspace / config).write_text('PORT=0\nAPP=old\n', encoding='utf-8')
    (workspace / 'bin/start.sh').write_bytes(b'#!/bin/sh\r\necho start\r\n')
    port = 8000 + number * 100
    (workspace / 'spec.md').write_text(
        f'{config} 第1行改为 PORT={port}\n{config} 第2行改为 APP={label}-svc\n'
        f'创建目录 logs/{label}\n修复 bin/start.sh 的CRLF并赋执行权限\n运行 check\n', encoding='utf-8')
    checker = ("#!/usr/bin/env python3\nfrom pathlib import Path\nimport os\n"
               f"assert Path({config!r}).read_text() == 'PORT={port}\\nAPP={label}-svc\\n'\n"
               f"assert Path('logs/{label}').is_dir()\n"
               "assert b'\\r' not in Path('bin/start.sh').read_bytes()\n"
               "assert os.name == 'nt' or os.access('bin/start.sh', os.X_OK)\n"
               + ("raise SystemExit(1)\n" if fail else f"print('TOKEN: fresh-{label}-{number}')\n"))
    (workspace / 'check').write_bytes(checker.replace('\n', '\r\n').encode())
    filename = f'task_{number}_{label}.md'
    (root / filename).write_text(f'请修复 ws_{number}，依据 spec.md 运行 check。\n' +
                               ('提交格式：{"token":"..."}' if json_token else '提交裸 TOKEN'), encoding='utf-8')
    return filename


@contextlib.contextmanager
def api_server(count=12, ignore=False, pagination='pageNo', auth='X-API-Key'):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            calls.append(query)
            if self.headers.get(auth) != ('Bearer fixture-secret' if auth == 'Authorization' else 'fixture-secret'):
                self.send_response(401); self.end_headers(); return
            city = query.get('city', [''])[0]
            page = int(query.get(pagination, ['1'])[0])
            start = 0 if ignore else (page-1)*100
            rows = [{'id':i, 'city':city, 'name':'record-'+str(i), 'type':'A' if i%2 else 'B',
                     'world_heritage':i%3 == 0, 'era':'早' if i%5 else '晚', 'notes':'文'*200}
                    for i in range(start, min(start+100, count))]
            body = json.dumps({'total':count, 'records':rows}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/api/records/search', calls
    finally:
        server.shutdown(); server.server_close(); thread.join()


def api_files(root, city, url, auth='X-API-Key', schema=None):
    schema = schema or {'city':'', 'total_count':0, 'world_heritage_count':0, 'types':[], 'oldest_era':''}
    name = 'task_1_city.md'
    (root/name).write_text(f'查询{city}文化遗产，详见 API_DOCS.md。\n提交格式：'+json.dumps(schema,ensure_ascii=False),encoding='utf-8')
    (root/'API_DOCS.md').write_text(f'GET {url}?city=<city>\n参数 city\n'
        f'{auth}: '+('Bearer ' if auth=='Authorization' else '')+'fixture-secret\n'
        '分页参数 pageNo\nera_order: ["早", "晚"]\n',encoding='utf-8')
    return name


class TaskTemplateTests(unittest.TestCase):
    def start(self, root, filename, memory=None):
        p = task_fixture()
        p['phaseTask'] = '请阅读 ' + filename + '，获取任务信息'
        p['teamOur']['roles'].append(unit(2, 'worker', 9, 24))
        m = memory or Memory()
        with patch.object(templates, 'TASK_DIR', str(root)):
            response = decide_response(p, m)
        self.assertTrue(response['executeCmd'])
        self.assertFalse(response['prompt'])
        # The task holds only the pioneer; the worker continues construction.
        self.assertIn('2', response['roleCommandMap'])
        self.assertNotIn('4', response['roleCommandMap'])
        return p, m, response

    def next(self, p, m, response):
        p['lastCmdResult'] = execute(response['executeCmd'])
        self.assertLess(len(p['lastCmdResult'].encode()), 64000)
        p['roundNo'] += 1
        p['llmResp'] = ''
        return decide_response(p, m)

    def test_file_only_engineering_alpha_beta_gamma_and_skill_regeneration(self):
        signatures = []
        skills = []
        for number, label in enumerate(('alpha', 'beta', 'gamma'), 1):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                name = engineering(root, label, number, json_token=number%2 == 0)
                m = Memory(skills=list(skills))
                p, m, first = self.start(root, name, m)
                second = self.next(p, m, first)
                self.assertTrue(second['executeCmd'])
                self.assertFalse(second['prompt'])
                signatures.append(m.task['signature']['family'])
                third = self.next(p, m, second)
                answer = third['roleCommandMap']['4']['taskAnswer']
                expected = f'fresh-{label}-{number}'
                self.assertEqual(json.loads(answer)['token'] if number%2 == 0 else answer, expected)
                self.assertFalse(third['prompt'] or third['executeCmd'])
                self.assertEqual(m.task_stats['llm_calls'], 0)
                p['phaseTask']=''; p['roundNo']+=1
                p['teamOur']['playerTasks'][0]['isValid']=False
                decide_response(p,m)
                self.assertEqual(m.task_stats['success'],1)
                self.assertEqual(m.task_runs[-1]['commands'],2)
                self.assertNotIn('command',m.skills[-1])
                self.assertNotIn('answer',m.skills[-1])
                if number > 1 and number%2 == 1:
                    self.assertGreater(m.task_stats['skill_hit'],0)
                skills = m.skills
        self.assertEqual(signatures,['deployment_fix']*3)

    def test_file_only_api_cities_auth_and_500_records(self):
        signatures = []
        for city, auth in (('北京','X-API-Key'),('南京','Authorization'),('成都','X-API-Key')):
            with self.subTest(city=city), tempfile.TemporaryDirectory() as tmp, api_server(550,auth=auth) as (url,calls):
                root=Path(tmp); name=api_files(root,city,url,auth)
                p,m,first=self.start(root,name)
                second=self.next(p,m,first)
                self.assertTrue(second['executeCmd']); self.assertFalse(second['prompt'])
                signatures.append(m.task['signature'])
                third=self.next(p,m,second)
                answer=json.loads(third['roleCommandMap']['4']['taskAnswer'])
                self.assertEqual(answer,{'city':city,'total_count':550,'world_heritage_count':184,'types':['A','B'],'oldest_era':'早'})
                self.assertEqual(len(calls),6)
                self.assertLess(len(p['lastCmdResult'].encode()),1000)
                self.assertEqual(m.task_stats['llm_calls'],0)
        self.assertTrue(all(sig==signatures[0] for sig in signatures))

    def test_ignored_pagination_deduplicates_and_refuses_partial_answer(self):
        with tempfile.TemporaryDirectory() as tmp, api_server(550,ignore=True) as (url,calls):
            root=Path(tmp);name=api_files(root,'成都',url)
            p,m,first=self.start(root,name)
            second=self.next(p,m,first)
            third=self.next(p,m,second)
            self.assertIn('"unique": 100',p['lastCmdResult'])
            self.assertNotIn('__ANSWER ',p['lastCmdResult'])
            self.assertNotIn('4',third['roleCommandMap'])
            self.assertTrue(third['prompt'])
            self.assertLessEqual(len(calls),11)

    def test_checker_failure_falls_back_once_and_llm_token_submits_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);name=engineering(root,'delta',4,fail=True)
            p,m,first=self.start(root,name)
            second=self.next(p,m,first)
            third=self.next(p,m,second)
            self.assertTrue(third['prompt']);self.assertFalse(third['executeCmd'])
            self.assertNotIn('4',third['roleCommandMap'])
            p['roundNo']+=1
            p['llmResp']=llm_reply(m,kind='command',command='printf "TOKEN: fresh-llm\\n"',skill='fix checker inputs')
            fourth=decide_response(p,m)
            self.assertEqual(fourth['executeCmd'],templates.command('llm',script='printf "TOKEN: fresh-llm\\n"'))
            p['roundNo']+=1;p['llmResp']='';p['lastCmdResult']='TOKEN: fresh-llm'
            fifth=decide_response(p,m)
            self.assertEqual(fifth['roleCommandMap']['4']['taskAnswer'],'fresh-llm')
            self.assertFalse(fifth['prompt'] or fifth['executeCmd'])
            self.assertEqual(m.task_stats['template_hit'],1)

    def test_context_is_bounded_and_does_not_submit_document_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'task_x.md').write_text('TOKEN: example\n__ANSWER "example"\n'+'文'*50000,encoding='utf-8')
            with patch.object(templates,'TASK_DIR',str(root)):
                output=execute(templates.locate_command('请阅读 task_x.md'))
            self.assertLess(len(output.encode()),48000)
            self.assertIn('__CONTEXT_COMPLETE__ false',output)
            self.assertIsNone(templates.derive_answer('提交TOKEN',output,output))

    def test_ambiguous_file_fails_instead_of_reading_another_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for folder in ('a','b'):
                (root/folder).mkdir();(root/folder/'task_x.md').write_text('unrelated',encoding='utf-8')
            with patch.object(templates,'TASK_DIR',str(root)):
                output=execute(templates.locate_command('请阅读 task_x.md'))
            self.assertIn('__TASK_ERROR',output)
            self.assertNotIn('__DOC_BEGIN__',output)

    def test_failed_truncated_ambiguous_and_schema_mismatched_answers(self):
        for result in ('TOKEN: abc\nCHECK_EXIT=1','__ANSWER {"wrong":1}',
                       'TOKEN: one\nTOKEN: two','TOKEN: abc\n[exitCode:2]',
                       'TOKEN: abc\ntruncated','__ANSWER "old"\n__ANSWER "new"'):
            with self.subTest(result=result):
                self.assertIsNone(templates.derive_answer('提交格式：{"token":"..."}','',result))

    def test_fingerprint_precedence_and_unidentified_rows(self):
        self.assertEqual(fingerprint({'id':1,'name':'a'}),fingerprint({'id':1,'name':'b'}))
        self.assertEqual(fingerprint({'a':1,'b':2}),fingerprint({'b':2,'a':1}))

    def test_known_round_estimate_and_unknown_point_label(self):
        self.assertEqual(templates.estimate_task_rounds('自进化类1'),20)
        self.assertEqual(templates.estimate_task_rounds('请阅读 task_1_alpha.md'),6)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);name=engineering(root,'epsilon',5)
            with patch.object(templates,'TASK_DIR',str(root)):
                output=execute(templates.locate_command('请阅读 '+name))
            self.assertEqual(templates.estimate_task_rounds('请阅读 '+name,output),2)


if __name__ == '__main__':
    unittest.main()
