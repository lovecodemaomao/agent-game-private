"""Bounded standard-library helpers shipped as source to the judge sandbox.

Importing this module performs no I/O. Only the generated executeCmd runs main.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

OUTPUT_LIMIT = 48000
OUTPUT_USED = 0
PAGE_NAMES = ('page', 'page_no', 'pageNo', 'pageNum', 'page_num', 'pageIndex',
              'p', 'offset', 'skip', 'start')


def emit(marker, value):
    global OUTPUT_USED
    text = marker + ' ' + json.dumps(value, ensure_ascii=False)
    if OUTPUT_USED + len(text.encode('utf-8')) > OUTPUT_LIMIT - 100:
        text = '__TASK_ERROR "output limit exceeded"'
    OUTPUT_USED += len(text.encode('utf-8')) + 1
    if OUTPUT_USED > OUTPUT_LIMIT:
        return
    print(text)


def inside(path, root):
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('path outside task root')
    return resolved


def walk(root, depth=3):
    """Never follow directory symlinks; bound both depth and visited entries."""
    count = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        current = Path(folder)
        dirs[:] = sorted(d for d in dirs if not (current / d).is_symlink())
        if len(current.relative_to(root).parts) >= depth:
            dirs[:] = []
        for name in sorted(files):
            count += 1
            if count > 2048:
                return
            p = current / name
            if not p.is_symlink():
                yield p


def collect(args):
    root = Path(args['root']).resolve()
    if root == Path(root.anchor) or not root.is_dir():
        raise ValueError('invalid task directory')
    names = args.get('names', [])
    files = list(walk(root))
    target = None
    if names:
        # Exact path wins; duplicate basenames must not silently pick another task.
        for name in names:
            candidate = inside(root / name, root)
            if candidate.is_file():
                target = candidate
                break
            matches = [p for p in files if p.name == Path(name).name]
            if len(matches) == 1:
                target = matches[0]
                break
            if len(matches) > 1:
                raise ValueError('ambiguous task filename')
        if target is None:
            raise ValueError('task file not found within search depth')
    if target:
        with target.open('rb') as stream:
            task_text = stream.read(65536).decode('utf-8', 'replace')
    else:
        task_text = ''
    folder = target.parent if target else root
    workspaces = re.findall(r'(?:[\w./-]+/)?ws[_-]\d+', task_text)
    roots = []
    for value in workspaces:
        p = inside(folder / value, root)
        if p.is_dir() and p not in roots:
            roots.append(p)
    nearby = list(walk(folder, 0))
    for workspace in roots:
        nearby.extend(walk(workspace, 2))
    if not roots and (folder / 'spec.md').is_file():
        nearby.extend(p for p in walk(folder, 2)
                      if not any(re.fullmatch(r'ws[_-]\d+', part)
                                 for part in p.relative_to(folder).parts[:-1]))
    extensions = {'.md', '.txt', '.json', '.conf', '.cfg', '.ini', '.yaml', '.yml', '.sh'}
    candidates = [p for p in dict.fromkeys(nearby)
                  if p.suffix.lower() in extensions or p.name in ('check', 'checker')
                  or p.name.lower().startswith('readme')]
    candidates = [p for p in candidates if p == target or not p.name.startswith('task_')]
    candidates.sort(key=lambda p: (p != target, p.name not in ('spec.md', 'API_DOCS.md'), str(p)))
    if target and target not in candidates:
        candidates.insert(0, target)
    parts = ['__TASK_FILE__ ' + json.dumps(str(target) if target else ''),
             '__TASK_DIR__ ' + json.dumps(str(folder))]
    used = len('\n'.join(parts).encode('utf-8'))
    complete = True
    for p in candidates[:20]:
        with p.open('rb') as stream:
            data = stream.read(65537)
        # Budget encoded JSON bytes, including escaping and multibyte characters.
        text = data[:65536].decode('utf-8', 'replace')
        truncated = len(data) > 65536
        while True:
            record = json.dumps({'path': str(p), 'content': text, 'truncated': truncated}, ensure_ascii=False)
            block = '\n__DOC_BEGIN__ ' + record + '\n__DOC_END__'
            if used + len(block.encode('utf-8')) <= 45000 or not text:
                break
            text = text[:max(0, len(text) - max(256, len(text)//4))]
            truncated = True
        if used + len(block.encode('utf-8')) > 45000:
            complete = False
            break
        parts.append(block)
        used += len(block.encode('utf-8'))
        complete &= not truncated
        if truncated:
            break
    complete &= len(candidates) <= 20
    parts.append('__CONTEXT_COMPLETE__ ' + json.dumps(bool(complete)))
    print('\n'.join(parts))


def run_bounded(command, cwd, deadline):
    """Bound child lifetime and retained output, including shell descendants."""
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=os.name != 'nt')
        try:
            code = child.wait(timeout=max(.05, deadline-time.monotonic()))
        finally:
            if os.name != 'nt':
                # Also reap descendants left behind by a checker/shell that exited.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif child.poll() is None:
                child.kill()
            child.wait()
        size = output.tell()
        output.seek(0)
        text = output.read(40000).decode('utf-8', 'replace')
    print(text)
    if code or size > 40000:
        emit('__TASK_ERROR', 'command failed or output truncated')
    return code


def deploy(args, deadline):
    root = Path(args['workspace']).resolve()
    if not root.is_dir() or root == Path(root.anchor):
        raise ValueError('invalid workspace')
    # Validate all edits before applying any of them; no shell interpolation.
    changes = {}
    for filename, number, value in args['edits']:
        path = inside(root / filename, root)
        if path.stat().st_size > 65536:
            raise ValueError('config too large')
        lines = changes.setdefault(path, path.read_text(encoding='utf-8').splitlines())
        if not 1 <= number <= len(lines):
            raise ValueError('config line out of range')
        lines[number-1] = value
    scripts = [inside(root / name, root) for name in args['scripts']]
    checker = inside(root / args['checker'], root)
    for p in dict.fromkeys(scripts + [checker]):
        if not p.is_file() or p.stat().st_size > 65536:
            raise ValueError('invalid script')
    for name in args['directories']:
        path = inside(root / name, root)
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)
    for path, lines in changes.items():
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    for path in dict.fromkeys(scripts + [checker]):
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        path.chmod(path.stat().st_mode | 0o111)
    first = checker.read_text(encoding='utf-8').splitlines()[0]
    command = ([sys.executable, str(checker)] if first.startswith('#!') and 'python' in first
               else ['sh', str(checker)] if first.startswith('#!') and re.search(r'\b(?:ba)?sh\b', first)
               else [str(checker)])
    print('CHECK_EXIT=' + str(run_bounded(command, root, deadline)))


def fingerprint(row):
    for key in ('id', 'uuid', 'name'):
        if row.get(key) is not None:
            return key + ':' + json.dumps(row[key], sort_keys=True, ensure_ascii=False)
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def unpack(raw):
    if isinstance(raw, list):
        return raw, None, None
    if not isinstance(raw, dict):
        return None, None, None
    total = next((raw[k] for k in ('total', 'total_count', 'totalCount')
                  if isinstance(raw.get(k), int) and not isinstance(raw[k], bool)), None)
    more = raw.get('has_more', raw.get('hasMore'))
    for name in ('records', 'items', 'rows', 'results', 'data', 'list', 'result', 'content'):
        value = raw.get(name)
        if isinstance(value, list):
            return value, total, more
        if isinstance(value, dict):
            rows, nested_total, nested_more = unpack(value)
            if rows is not None:
                return rows, total if total is not None else nested_total, more if more is not None else nested_more
    return None, total, more


def aggregate(rows, args):
    """Only supported fields with complete, consistently typed evidence."""
    keys = args.get('answer_keys', [])
    if not keys:
        return None
    answer = {}
    for key in keys:
        if key == 'city' and args.get('city'):
            answer[key] = args['city']
        elif key in ('total_count', 'total', 'count'):
            answer[key] = len(rows)
        elif key in ('types', 'categories'):
            field = 'type' if key == 'types' else 'category'
            if not all(isinstance(r.get(field), str) for r in rows):
                return None
            answer[key] = sorted({r[field] for r in rows})
        elif key == 'world_heritage_count':
            field = next((f for f in ('world_heritage', 'is_world_heritage')
                          if rows and all(isinstance(r.get(f), bool) for r in rows)), None)
            if field is None:
                return None
            answer[key] = sum(r[field] for r in rows)
        elif key == 'oldest_era':
            order = args.get('era_order', [])
            if not rows or not order or not all(r.get('era') in order for r in rows):
                return None
            answer[key] = min((r['era'] for r in rows), key=order.index)
        else:
            return None
    return answer


def harvest(args, deadline):
    parsed = urllib.parse.urlsplit(args['url'])
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(args.get('query', {}))
    headers = args.get('headers', {})
    calls = 0

    def fetch(extra):
        nonlocal calls
        if calls >= 40 or time.monotonic() >= deadline:
            raise TimeoutError('API budget exhausted')
        calls += 1
        q = {k: v for k, v in query.items() if not extra or k not in PAGE_NAMES}
        q.update(extra)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                      urllib.parse.urlencode(q), ''))
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=min(1.5, max(.05, deadline-time.monotonic()))) as response:
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError('API page too large')
        return json.loads(data)

    initial = fetch({})
    rows, total, more = unpack(initial)
    if rows is None:
        # Flat APIs may return exactly the documented answer object.
        keys = args.get('answer_keys', [])
        if keys and isinstance(initial, dict) and all(k in initial for k in keys):
            emit('__ANSWER', {k: initial[k] for k in keys})
        else:
            emit('__API_SAMPLE', str(initial)[:2000])
        return
    found = {}

    def add(batch):
        if not all(isinstance(row, dict) for row in batch):
            raise ValueError('records must be objects')
        before = len(found)
        for row in batch:
            found.setdefault(fingerprint(row), row)
        if len(found) > 10000:
            raise ValueError('record budget exhausted')
        return len(found)-before

    add(rows)
    complete = (total is not None and len(found) == total) or more is False
    candidates = list(dict.fromkeys(args.get('pagination', []) + list(PAGE_NAMES)))
    invalid = []
    for name in candidates:
        if complete or time.monotonic() >= deadline:
            break
        page, distinct_pages = 1, 0
        while not complete and page < 30 and time.monotonic() < deadline:
            value = len(found) if name in ('offset', 'skip', 'start') else page + (0 if name == 'pageIndex' else 1)
            try:
                batch, observed_total, observed_more = unpack(fetch({name: value}))
            except (OSError, ValueError, TimeoutError):
                break
            if batch is None:
                break
            added = add(batch)
            # A server ignoring a pagination key must never inflate the total.
            if not added:
                if not batch and distinct_pages and total is None:
                    complete = True
                else:
                    invalid.append(name)
                break
            distinct_pages += 1
            if total is not None and observed_total not in (None, total):
                raise ValueError('API total changed during pagination')
            complete = (total is not None and len(found) == total) or observed_more is False
            page += 1
        if distinct_pages:
            # Do not mix two working pagination schemes after partial progress.
            break
    rows = list(found.values())
    if total is not None and len(rows) != total:
        complete = False
    if args.get('city') and any(r.get('city', args['city']) != args['city'] for r in rows):
        complete = False
    answer = aggregate(rows, args) if complete else None
    if answer is not None:
        emit('__ANSWER', answer)
        return
    emit('__API_META', {'complete': complete, 'calls': calls, 'invalid_pagination': invalid})
    emit('__API_TOTAL', {'unique': len(rows), 'expected': total})
    emit('__API_KEYS', sorted({k for row in rows for k in row})[:50])
    distributions = {}
    for key in args.get('answer_keys', []):
        field = {'types': 'type', 'categories': 'category', 'oldest_era': 'era'}.get(key, key)
        values = {}
        for row in rows:
            value = json.dumps(row.get(field), ensure_ascii=False)[:100]
            values[value] = values.get(value, 0) + 1
        distributions[field] = dict(list(values.items())[:30])
    emit('__API_DIST', distributions)
    emit('__API_SAMPLE', [{k: str(v)[:120] for k, v in list(r.items())[:12]} for r in rows[:10]])


def main(args):
    global OUTPUT_USED
    OUTPUT_USED = 0
    sys.stdout.reconfigure(encoding='utf-8')
    deadline = time.monotonic() + 11
    try:
        if hasattr(signal, 'SIGALRM'):
            def expire(*_):
                raise TimeoutError('sandbox helper deadline')
            signal.signal(signal.SIGALRM, expire)
            signal.setitimer(signal.ITIMER_REAL, 12)
        {'context': lambda: collect(args), 'deployment_fix': lambda: deploy(args, deadline),
         'record_api': lambda: harvest(args, deadline),
         'llm': lambda: run_bounded(['sh', '-c', args['script']], None, deadline)}[args['mode']]()
    except Exception as exc:
        # Do not echo credentials, URLs with secrets, or arbitrary exception text.
        emit('__TASK_ERROR', type(exc).__name__)
    finally:
        if hasattr(signal, 'SIGALRM'):
            signal.setitimer(signal.ITIMER_REAL, 0)


if 'PAYLOAD' in globals():
    main(PAYLOAD)
