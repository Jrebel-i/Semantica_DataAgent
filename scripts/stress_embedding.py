#!/usr/bin/env python3
"""Exercise the shared embedding service; use only on an agreed test window."""
import argparse
import concurrent.futures
import json
import subprocess
import threading
import time
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://172.16.242.124:31603')
parser.add_argument('--ssh', default='baize@172.16.242.124')
parser.add_argument('--clients', type=int, default=3)
parser.add_argument('--batches', type=int, default=2)
parser.add_argument('--chars', type=int, default=1888)
parser.add_argument('--timeout', type=int, default=900)
args = parser.parse_args()
if min(args.clients, args.batches, args.chars, args.timeout) < 1:
    parser.error('numeric arguments must be positive')

def pod_status():
    try:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', args.ssh,
            'kubectl -n semantica-dataagent get pods -l app=embedding -o json'],
            capture_output=True, text=True, timeout=15, check=True)
        pods = json.loads(result.stdout)['items']
        return [(pod['metadata']['name'], status['restartCount'],
                 status.get('lastState', {}).get('terminated', {}).get('reason', ''))
                for pod in pods for status in pod.get('status', {}).get('containerStatuses', [])
                if status['name'] == 'infinity']
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        print('status error:', exc, flush=True)
        return []

stop = threading.Event()
samples = []
def monitor():
    while not stop.is_set():
        try:
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', args.ssh,
                'kubectl -n semantica-dataagent top pod -l app=embedding --no-headers'],
                capture_output=True, text=True, timeout=15, check=True)
            for line in result.stdout.splitlines():
                memory = line.split()[-1]
                if memory.endswith('Mi'):
                    samples.append(float(memory[:-2]))
                elif memory.endswith('Gi'):
                    samples.append(float(memory[:-2]) * 1024)
                print('sample:', line, flush=True)
        except (subprocess.SubprocessError, OSError) as exc:
            print('sample error:', exc, flush=True)
        stop.wait(5)

def client(client_id):
    results = []
    for batch in range(args.batches):
        phrase = '数仓业务指标订单用户数据统计分析口径时间维度字段表关联查询模型。'
        text = (phrase * (args.chars // len(phrase) + 1))[:args.chars]
        payload = json.dumps({'model': 'model/bge-m3', 'input': [f'{client_id}-{batch}-{i} {text}' for i in range(32)]}, ensure_ascii=False).encode()
        request = urllib.request.Request(args.url + '/embeddings', data=payload, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                data = json.load(response)['data']
                if len(data) != 32 or any(len(item['embedding']) != 1024 for item in data):
                    raise ValueError('unexpected embedding shape')
            results.append(True)
            print(f'client {client_id} batch {batch}: success', flush=True)
        except Exception as exc:
            results.append(False)
            print(f'client {client_id} batch {batch}: {type(exc).__name__}: {exc}', flush=True)
    return results

before = pod_status()
print('pods before:', before, flush=True)
start = time.monotonic()
thread = threading.Thread(target=monitor)
thread.start()
try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.clients) as pool:
        results = [value for result in pool.map(client, range(args.clients)) for value in result]
finally:
    stop.set()
    thread.join()
elapsed = time.monotonic() - start
after = pod_status()
print('pods after:', after, flush=True)
print(f'result: {sum(results)}/{len(results)} successful; elapsed={elapsed:.1f}s; samples={len(samples)}; sampled peak={max(samples, default=0):g} MiB', flush=True)
raise SystemExit(0 if all(results) and samples and before and after and before == after else 1)
