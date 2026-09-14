"""Compare routing work on identical decisions: python -B benchmark_routing.py [repo]."""
import hashlib
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
sys.path[:0] = [str(root / 'Demo/CoreGeek/src'), str(root / 'Demo/CoreGeek/tests')]
from test_issue4_efficiency import payload
from agent.brain import decide_response
from agent.grid import Routes


def main():
    responses = []
    start = time.perf_counter()
    with patch('agent.brain.Routes', wraps=Routes) as build:
        for _ in range(5):
            for gold in (40, 200, 400):
                responses.append(decide_response(payload(day=2, gold=gold)))
        print(json.dumps({
            'decisions': len(responses), 'bfs_builds': build.call_count,
            'seconds': round(time.perf_counter() - start, 3),
            'response_hash': hashlib.sha256(json.dumps(responses, sort_keys=True).encode()).hexdigest(),
        }))


if __name__ == '__main__':
    main()
