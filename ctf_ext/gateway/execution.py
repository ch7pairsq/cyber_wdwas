"""可信命令执行边界；工作目录隔离不是容器安全沙箱，正式执行必须走 SandboxSpec。"""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass


def capture(argv, *, cwd, timeout, limit=200_000, input_data=b''):
    """有界采集并在截止时杀进程组；stdin 是已限额的本地附件。"""
    import tempfile
    if timeout <= 0 or limit < 1 or len(input_data) > 1_000_000:
        raise ValueError('invalid command budget')
    started = time.monotonic()
    with tempfile.TemporaryFile() as source:
        source.write(input_data)
        source.seek(0)
        proc = subprocess.Popen(list(argv), cwd=cwd, stdin=source, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                env={'PATH': os.environ.get('PATH', os.defpath), 'LC_ALL': 'C.UTF-8'})
        output, total, timed_out, truncated = bytearray(), 0, False, False
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                left = timeout - (time.monotonic() - started)
                if left <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(left, .1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    output.extend(chunk[:max(0, limit - len(output))])
                    if total > limit:
                        truncated = True
                        break
                if truncated:
                    break
        finally:
            selector.close()
            # EOF 不等于父进程已退出；正常结果须先等待退出码，避免误杀成功命令。
            if not (timed_out or truncated):
                try:
                    proc.wait(timeout=max(.001, timeout - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    timed_out = True
            # 同时清理残留的同组子进程。
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
            proc.stdout.close()
    return {'output': bytes(output), 'returncode': proc.returncode,
            'complete': not (timed_out or truncated), 'timed_out': timed_out,
            'truncated': truncated, 'bytes': total}


@dataclass(frozen=True)
class SandboxSpec:
    image: str
    workspace: str
    name: str

    def argv(self, command):
        from pathlib import Path
        import re
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,80}', self.name):
            raise ValueError('invalid owned container name')
        if not re.fullmatch(r'(?:[a-zA-Z0-9][a-zA-Z0-9./:_-]*@)?sha256:[a-f0-9]{64}', self.image):
            raise ValueError('sandbox image must be pinned')
        root = Path(self.workspace)
        if not root.is_absolute() or root.is_symlink() or root.resolve() != root or not root.is_dir() or ',' in str(root):
            raise ValueError('invalid workspace mount')
        if not command or any(not isinstance(x, str) or '\x00' in x for x in command):
            raise ValueError('invalid argv')
        return ['docker', 'run', '--rm', '--pull', 'never', '--name', self.name,
                '--network', 'none', '--user', '10001:10001', '--read-only',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--memory', '512m', '--cpus', '1', '--pids-limit', '128',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=64m',
                '--mount', 'type=bind,src=' + str(root) + ',dst=/workspace',
                '--workdir', '/workspace', '--entrypoint', command[0], self.image, *command[1:]]


class CommandExecutor:
    """仅供受信任引擎适配器调用；LLM 文本不能伪造 command_log。"""
    def __init__(self, store, control, environment, *, allow_local_mock=False):
        self.store, self.control, self.environment = store, control, environment
        self.allow_local_mock = allow_local_mock

    def execute_mock_decoder(self, data, *, decode=True, target_revision='mock-attachment-v1'):
        # 只允许固定离线解码程序；不执行模型提供的 Python 或 shell。
        import sys
        if not self.allow_local_mock:
            raise PermissionError('local decoder is restricted to mock protocol')
        script = ('import base64,sys;sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read(),validate=True))'
                  if decode else 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())')
        argv = [sys.executable, '-I', '-c', script]
        version = 'python-' + sys.version.split()[0]
        input_hash = hashlib.sha256(data).hexdigest()
        cid = self.control.challenge_id
        key = self.store.cache_key(cid, target_revision, self.environment, 'fixture-decoder', version, argv, input_hash)
        self.control.reserve('fixture-decoder', version, argv, input_hash)
        cached = self.store.cached(key)
        if cached:
            return self.store.restore_evidence(cached['evidence_id']), cached['evidence_id'], cached['id'], True
        result = capture(argv, cwd=str(self.store.workspace(cid)), timeout=min(15, self.control.remaining()['seconds']), input_data=data)
        eid = self.store.evidence.put_bytes(result['output'], kind='command_output')
        command_id = 'cmd-' + uuid.uuid4().hex
        complete = result['complete'] and result['returncode'] == 0
        with self.store.board._write() as db:
            db.execute('INSERT INTO command_log VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (command_id, cid, eid, json.dumps(argv), version, target_revision,
                        self.environment, result['returncode'], int(complete), time.time()))
            if complete:
                db.execute('INSERT OR REPLACE INTO tool_cache VALUES(?,?,?)', (key, command_id, time.time() + 300))
        if complete:
            self.control.progress(eid, 'fixture-decoder:' + input_hash)
        else:
            raise ValueError('decoder failed, truncated or timed out; evidence cannot ground a flag')
        return result['output'], eid, command_id, False
