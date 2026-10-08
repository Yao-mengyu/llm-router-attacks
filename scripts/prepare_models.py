#!/usr/bin/env python3
"""Download the two fixed official GGUF models and a fixed llama.cpp runtime.

Only public model/runtime files are downloaded. No API keys are used. Model
inference itself stays on loopback. About 1.63 GB is downloaded on a clean run.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path

def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def download(url,path,expected_hash,expected_size):
    if path.exists():
        if path.stat().st_size==expected_size and sha256(path)==expected_hash:
            print('VERIFIED existing',path.name,flush=True);return
        raise RuntimeError(f'Existing file does not match the configured release: {path}; move it aside before retrying.')
    request=urllib.request.Request(url,headers={'User-Agent':'Router-Attack-Demo/0.3'})
    print('DOWNLOAD',path.name,expected_size,'bytes',flush=True)
    partial=None
    try:
        with urllib.request.urlopen(request,timeout=90) as r, tempfile.NamedTemporaryFile(
                dir=path.parent,prefix='.model-download-',suffix='.part',delete=False) as out:
            partial=Path(out.name)
            received=0
            while chunk:=r.read(min(8*1024*1024,expected_size-received+1)):
                received+=len(chunk)
                if received>expected_size:
                    raise RuntimeError(f'Download exceeded the configured size for {path.name}.')
                out.write(chunk)
        if partial.stat().st_size!=expected_size or sha256(partial)!=expected_hash:
            raise RuntimeError(f'Checksum mismatch for {path.name}; the partial file was not installed.')
        # Never replace a file created by another concurrent setup process.
        path.hardlink_to(partial)
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)
    print('VERIFIED downloaded',path.name,flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir',type=Path,default=Path('.runtime'))
    args=parser.parse_args()
    root=args.runtime_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    lock=json.loads((Path(__file__).resolve().parents[1]/'configs/local_models.lock.json').read_text())
    architecture=platform.machine()
    if architecture=='AMD64':architecture='x86_64'
    key=platform.system()+'-'+architecture
    if key not in lock['runtimes']:
        raise SystemExit('Automatic setup supports Linux x86_64 and macOS arm64/x86_64. On another platform, start two compatible local servers as described in docs/running.md.')
    runtime=lock['runtimes'][key];tag=lock['runtime_tag']
    runtime_url=f'https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{runtime["asset"]}'
    archive=root/runtime['asset']
    download(runtime_url,archive,runtime['sha256'],runtime['size'])
    extracted=root/'llama.cpp';extracted.mkdir(exist_ok=True)
    with tarfile.open(archive,'r:gz') as tar:
        # data_filter rejects absolute paths, traversal, devices and unsafe links.
        if not hasattr(tarfile,'data_filter'):
            raise SystemExit('Use a maintained Python with tarfile.data_filter (e.g. Python 3.12 or newer) for safe extraction.')
        tar.extractall(extracted,filter='data')
    matches=list(extracted.rglob('llama-server'))
    if len(matches)!=1:raise RuntimeError('Expected one llama-server executable in the official archive.')
    server=matches[0];server.chmod(server.stat().st_mode|0o100)
    provenance={'tag':tag,'commit':lock['runtime_commit'],'release_url':f'https://github.com/ggml-org/llama.cpp/releases/tag/{tag}','asset':runtime['asset'],'download_url':runtime_url,'sha256':runtime['sha256'],'server_relative':str(server.relative_to(root)),'platform':key}
    (root/'runtime-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    for size,model in lock['models'].items():
        download(model['url'],root/model['filename'],model['sha256'],model['size'])
        (root/f'qwen-{size}b-download.json').write_text(json.dumps(model,indent=2)+'\n')
    print('READY runtime directory:',root,flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
