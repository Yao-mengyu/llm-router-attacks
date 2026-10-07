#!/usr/bin/env python3
"""Start two pinned CPU checkpoints for a command in the same process tree.

Example from the repository root:
  python scripts/with_local_models.py -- python -m router_attack_demo run --backend local --out results/local.json

No model inference is sent outside this process's loopback network namespace.
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


class LocalQwenServers:
    def __init__(self, runtime_dir: Path | str | None = None):
        self.runtime_dir = Path(runtime_dir or '.runtime').resolve()
        self.processes: list[subprocess.Popen] = []
        self.records: list[dict] = []
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        self.log_dir = self.runtime_dir / 'logs' / stamp
        self.log_dir.mkdir(parents=True, exist_ok=False)

    def __enter__(self):
        runtime = json.loads((self.runtime_dir / 'runtime-provenance.json').read_text())
        server = self.runtime_dir / runtime.get('server_relative', 'llama.cpp/llama-' + runtime['tag'] + '/llama-server')
        try:
            for size, port in [('0.5', 8082), ('1.5', 8081)]:
                alias = f'qwen2.5-{size}b-instruct'
                model = self.runtime_dir / f'qwen2.5-{size}b-instruct-q4_k_m.gguf'
                metadata = json.loads((self.runtime_dir / f'qwen-{size}b-download.json').read_text())
                if model.stat().st_size != metadata['size']:
                    raise ValueError(f'Model file size does not match verified download: {model}')
                args = [str(server), '--model', str(model), '--host', '127.0.0.1',
                        '--port', str(port), '--alias', alias, '--n-gpu-layers', '0',
                        '--threads', '4', '--ctx-size', '2048', '--parallel', '1', '--metrics']
                logfile = self.log_dir / f'{alias}.log'
                with logfile.open('w') as output:
                    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output,
                                               stderr=subprocess.STDOUT)
                self.processes.append(process)
                record = {'model': alias, 'port': port, 'pid': process.pid,
                          'base_url': f'http://127.0.0.1:{port}/v1', 'args': args,
                          'log': str(logfile), 'checkpoint': metadata}
                self.records.append(record)
                deadline = time.monotonic() + 45
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f'{alias} exited during startup; see {logfile}')
                    try:
                        with self.opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                            healthy = response.status == 200
                        if healthy:
                            break
                    except Exception:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'{alias} did not become healthy; see {logfile}')
                    time.sleep(0.1)
                with self.opener.open(f'http://127.0.0.1:{port}/v1/models', timeout=3) as response:
                    listed = json.load(response)
                ids = [entry['id'] for entry in listed['data']]
                if alias not in ids:
                    raise RuntimeError(f'Expected exact API alias {alias}; got {ids}')
                record['api_model_listing'] = listed
                print(f'READY {alias} http://127.0.0.1:{port}/v1', flush=True)
            manifest = {'runtime': runtime, 'servers': self.records,
                        'network': '127.0.0.1 only, same process tree',
                        'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
            (self.log_dir / 'launch.json').write_text(json.dumps(manifest, indent=2) + '\n')
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('Supply the local command after --')
    with LocalQwenServers(args.runtime_dir) as servers:
        print('SERVER_LOGS', servers.log_dir, flush=True)
        return subprocess.run(command, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
