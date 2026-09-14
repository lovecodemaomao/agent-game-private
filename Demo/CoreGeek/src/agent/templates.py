"""Evidence-driven task plans; unknown or ambiguous instructions use the LLM."""
import base64
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import zlib

TASK_DIR = '/tmp/selfEvolutionTask'
FILE_RE = re.compile(r'[\w./-]+\.(?:md|txt|json|conf|cfg|ini|sh)')
URL_RE = re.compile(r'https?://[^\s`\"\'，。；<>]+(?:<[^>]+>)?[^\s`\"\'，。；]*')


@lru_cache(maxsize=1)
def runner():
    return base64.b64encode(zlib.compress(Path(__file__).with_name('task_sandbox.py').read_bytes(), 9)).decode()


def command(mode, **args):
    payload = base64.b64encode(json.dumps({'mode': mode, **args}, ensure_ascii=False).encode()).decode()
    source = ("import base64,json,zlib;PAYLOAD=json.loads(base64.b64decode(" + repr(payload) + "));"
              "exec(zlib.decompress(base64.b64decode(" + repr(runner()) + ")))")
    return 'python3 -c ' + shlex.quote(source)


def locate_command(desc):
    names = list(dict.fromkeys(FILE_RE.findall(desc)))[:5]
    return command('context', root=TASK_DIR, names=names)


def documents(dump):
    docs = []
    for line in str(dump or '').splitlines():
        if line.startswith('__DOC_BEGIN__ '):
            try:
                doc = json.loads(line[len('__DOC_BEGIN__ '):])
                if isinstance(doc, dict) and isinstance(doc.get('content'), str):
                    docs.append(doc)
            except ValueError:
                pass
    return docs


def corpus(desc, dump=''):
    docs = documents(dump)
    return desc + '\n' + ('\n'.join(d['content'] for d in docs) if docs else dump)


def task_text(desc, dump=''):
    """Prefer the task document over examples in API documentation."""
    docs = documents(dump)
    task = next((d for d in docs if PurePosixPath(d['path'].replace('\\', '/')).name.startswith('task_')), None)
    return desc + '\n' + (task['content'] if task else dump)


def answer_keys(text):
    # Only JSON following an explicit answer-schema label; never arbitrary config.
    match = re.search(r'(?:答案格式|答题格式|提交格式|返回格式|answer(?:_schema|\s+schema|\s+format))\s*[:：]?\s*(?:```(?:json)?\s*)?(\{[^{}]*\})', text, re.I)
    if not match:
        return []
    try:
        obj = json.loads(match.group(1))
        return list(obj) if obj and len(obj) <= 16 and all(isinstance(k, str) for k in obj) else []
    except ValueError:
        return []


def city_name(text):
    match = re.search(r'(?:city|location|城市)\s*[:=：]\s*["\'`]?([^\s"\'`，。；;&}]+)', text, re.I)
    if match:
        return match.group(1)
    match = re.search(r'(?:查询|统计|获取)\s*([^\s，。；]{2,12}?)(?:的|全部|所有|文化遗产|文物|今天|天气)', text)
    return match.group(1) if match else ''


def deployment_spec(desc, dump=''):
    docs = documents(dump)
    text = corpus(desc, dump)
    if '__CONTEXT_COMPLETE__ false' in dump or any(d.get('truncated') for d in docs):
        return None
    specs = [d for d in docs if PurePosixPath(d['path'].replace('\\', '/')).name == 'spec.md']
    if len(specs) > 1:
        return None
    spec = specs[0] if specs else None
    workspace = str(PurePosixPath(spec['path'].replace('\\', '/')).parent) if spec else ''
    if not workspace:
        root = re.search(r'(/[\w./-]*ws[_-]\d+)(?:/|\b)', text)
        workspace = root.group(1) if root else ''
    if not workspace:
        return None
    # Parse instructions, never source code examples from check/config/scripts.
    instructions = task_text(desc, dump) + '\n' + (spec['content'] if spec else '')
    edits = []
    for match in re.finditer(r'([\w./-]+\.(?:conf|cfg|ini))\s*(?:的\s*)?第\s*(\d+)\s*行\s*(?:改为|修改为|设置为|替换为)\s*[：:]?\s*([^\n；]+)', instructions):
        value = match.group(3).strip().strip('`').rstrip('。')
        if not value or '\n' in value:
            return None
        edits.append((match.group(1), int(match.group(2)), value))
    checker = re.search(r'(?:运行|执行|run)\s*[`\s]*(?:sh\s+|python3?\s+)?(?:\./)?(check(?:er)?)(?!\w)', instructions, re.I)
    if not edits or not checker:
        return None
    directories = list(dict.fromkeys(re.findall(r'(?:创建目录|mkdir\s+-p)\s*[`"\']?([\w./-]+)', instructions)))
    directories += [p for p in re.findall(r'\blogs/[\w.-]+', instructions) if p not in directories]
    scripts = list(dict.fromkeys(f for f in FILE_RE.findall(instructions) if f.endswith('.sh')))
    # Plans may only touch files within the explicitly identified workspace.
    paths = [p for p, _, _ in edits] + directories + scripts + [checker.group(1)]
    if any(PurePosixPath(p).is_absolute() or '..' in PurePosixPath(p).parts for p in paths):
        return None
    return dict(workspace=workspace, edits=edits, directories=directories, scripts=scripts, checker=checker.group(1))


def api_spec(desc, dump=''):
    text = corpus(desc, dump)
    primary = task_text(desc, dump)
    if '__CONTEXT_COMPLETE__ false' in dump:
        return None
    keys = answer_keys(primary) or answer_keys(text)
    urls = list(dict.fromkeys(u.rstrip(').,;') for u in URL_RE.findall(text)))
    if not keys or not urls:
        return None
    task_urls = URL_RE.findall(primary)
    if task_urls:
        urls = list(dict.fromkeys(task_urls))
    if len({urlsplit(u).path for u in urls if urlsplit(u).path not in ('', '/')}) > 1:
        return None
    # Prefer a documented request endpoint over a bare host. Do not invent paths.
    url = next((u for u in urls if urlsplit(u).path not in ('', '/')), None)
    if url is None:
        endpoint = re.search(r'\bGET\s+(/[\w/.-]+)', text)
        if not endpoint:
            return None
        url = urls[0].rstrip('/') + endpoint.group(1)
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    city = city_name(primary)
    city_key = next((k for k in query if k in ('city', 'location', 'region')), None)
    if city_key is None:
        match = re.search(r'(?:参数|parameter)\s*[:：]?\s*`?(city|location|region)\b', text, re.I)
        city_key = match.group(1) if match else None
    if city_key and city:
        query[city_key] = city
    elif city_key:
        value = query[city_key]
        if not value or any(c in value for c in '<>{}'):
            return None
        city = value
    elif city:
        return None
    if any(any(c in v for c in '<>{}') for v in query.values()):
        return None
    headers = {}
    for name, value in re.findall(r'\b(Authorization|X-API-Key)\s*:\s*["\'`]?((?:Bearer\s+)?[A-Za-z0-9_.~+/=-]+)', text, re.I):
        canonical = 'Authorization' if name.lower() == 'authorization' else 'X-API-Key'
        if canonical in headers and headers[canonical] != value:
            return None
        headers[canonical] = value
    for name, value in re.findall(r'\b(api_key|token|key)\s*[=:]\s*["\'`]?([A-Za-z0-9_.~+/-]+)', text):
        if name not in query:
            query[name] = value
    order = re.search(r'(?:era_order|年代顺序)\s*[:：]\s*(\[[^\]]+\])', text)
    try:
        era_order = json.loads(order.group(1)) if order else []
    except ValueError:
        return None
    if not isinstance(era_order, list) or not all(isinstance(v, str) for v in era_order):
        return None
    pagination = re.findall(r'\b(?:page_no|pageNo|pageNum|page_num|pageIndex|offset|skip|start|page|p)\b', text)
    return dict(url=urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')), query=query,
                city=city, headers=headers, answer_keys=keys, era_order=era_order, pagination=pagination)


@dataclass(frozen=True)
class TaskSignature:
    family: str
    answer_keys: tuple = ()
    files: tuple = ()
    operations: tuple = ()
    endpoint_shape: str = ''
    instruction_hash: str = ''

    def dump(self):
        # JSON-compatible fields are stable across memory serialization.
        return json.loads(json.dumps(asdict(self)))


def signature(desc, dump=''):
    deployment = deployment_spec(desc, dump)
    keys = tuple(sorted(answer_keys(task_text(desc, dump))))
    if deployment:
        return TaskSignature('deployment_fix', keys, ('spec.md', '*.conf', '*.sh', 'check'),
                             ('config_edit', 'mkdir', 'chmod', 'checker'))
    api = api_spec(desc, dump)
    if api:
        shape = re.sub(r'/\d+(?=/|$)', '/:id', urlsplit(api['url']).path)
        return TaskSignature('record_api', tuple(sorted(api['answer_keys'])), endpoint_shape=shape)
    # Unknown procedures are advisory only. Exact context hashes avoid accidental
    # matches after replacing arbitrary numbers, cities, ports or filenames.
    return TaskSignature('unknown', keys, instruction_hash=hashlib.sha256(corpus(desc, dump).encode()).hexdigest())


def plan(desc, dump=''):
    for family, builder in (('deployment_fix', deployment_spec), ('record_api', api_spec)):
        args = builder(desc, dump)
        if args:
            return family, command(family, **args)
    return None


def estimate_task_rounds(desc, dump=''):
    if plan(desc, dump):
        return 2
    # TaskPoint labels alone do not reveal the next random task's family.
    if re.search(r'\btask_[\w.-]+\.md\b', desc):
        return 6
    return 20


def derive_answer(desc, dump, result):
    text = str(result or '')
    # Context documents can contain example TOKEN/__ANSWER lines; they are not
    # evidence that a solver/checker succeeded in the current task.
    if any(marker in text for marker in ('__DOC_BEGIN__', '__TASK_FILE__', '__TASK_ERROR')):
        return None
    if re.search(r'(?:CHECK_EXIT\s*=\s*|exitCode\s*[:=]\s*)(?!0\b)-?\d+', text, re.I):
        return None
    if re.search(r'\b(?:timed out|timeout|truncated)\b|超时|截断|\[\s*FAIL\s*\]', text, re.I):
        return None
    keys = answer_keys(task_text(desc, dump)) or answer_keys(corpus(desc, dump))
    answers = re.findall(r'^__ANSWER\s+(.+)$', text, re.M)
    if answers:
        if len(set(answers)) != 1:
            return None
        try:
            value = json.loads(answers[0])
        except ValueError:
            return None
        if keys and (not isinstance(value, dict) or set(value) != set(keys)):
            return None
        if value is None or value == '':
            return None
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    tokens = re.findall(r'^\s*TOKEN\s*[:：=]\s*([A-Za-z0-9_.-]+)\s*$', text, re.M)
    if len(set(tokens)) == 1:
        if keys and keys != ['token']:
            return None
        json_token = keys == ['token'] or bool(re.search(r'JSON.{0,20}TOKEN|TOKEN.{0,20}JSON',
                                                        task_text(desc,dump), re.I))
        return json.dumps({'token': tokens[0]}) if json_token else tokens[0]
    # Compatibility for earlier harvest procedures, only with explicit proof of
    # completeness. A status=OK page alone is not a complete dataset.
    values = re.findall(r'^__API\s+(\{.+\})$',text,re.M)
    if len(values) == 1:
        try:
            value = json.loads(values[0])
            rows = value.get('records')
            if (value.get('status') == 'OK' and value.get('complete') is True
                    and isinstance(rows,list) and all(isinstance(r,dict) for r in rows)
                    and value.get('total',len(rows)) == len(rows)):
                from .task_sandbox import aggregate, fingerprint
                if len({fingerprint(r) for r in rows}) != len(rows):
                    return None
                args = api_spec(desc,dump)
                answer = aggregate(rows,args) if args else None
                if answer is not None:
                    return json.dumps(answer,ensure_ascii=False)
        except (ValueError,TypeError,AttributeError):
            pass
    return None
