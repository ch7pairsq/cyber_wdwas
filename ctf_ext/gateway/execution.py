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


_TOOL_ENV_KEYS = ('ARENA_PROXY', 'ARENA_NETWORK_CIDR', 'ARENA_BASE_URL',
                  'HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy',
                  'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy')


def tool_environment():
    """工具子进程环境：白名单精确到变量；代理值剥离 user:pass@。

    平台隧道（2026-11-20 官方提示词）：赛题靶机只经 SOCKS5/WireGuard
    可达，代理地址是路由配置而非凭据，放行给子进程；但代理自身的
    认证信息（若有）不进子进程。模型凭据（BAIZHI_API_KEY 等）仍绝不放行。
    """
    env = {'PATH': os.environ.get('PATH', os.defpath), 'LC_ALL': 'C.UTF-8'}
    for key in _TOOL_ENV_KEYS:
        value = os.environ.get(key)
        if value is None:
            continue
        if 'PROXY' in key.upper() and '@' in value:
            scheme, sep, rest = value.partition('://')
            if sep:
                value = scheme + '://' + rest.rpartition('@')[-1]
        env[key] = value
    return env


def capture(argv, *, cwd, timeout, limit=200_000, input_data=b'', discard_stderr=False):
    """有界采集并在截止时杀进程组；stdin 是已限额的本地附件。"""
    import tempfile
    if timeout <= 0 or limit < 1 or len(input_data) > 1_000_000:
        raise ValueError('invalid command budget')
    started = time.monotonic()
    with tempfile.TemporaryFile() as source:
        source.write(input_data)
        source.seek(0)
        proc = subprocess.Popen(list(argv), cwd=cwd, stdin=source, stdout=subprocess.PIPE,
                                # 固定解码只取 stdout；调试器可能把启动告警写到 stderr，
                                # 合并后会被当作命令输出污染证据。
                                stderr=subprocess.DEVNULL if discard_stderr else subprocess.STDOUT,
                                start_new_session=True,
                                # 环境白名单精确到工具所需变量：arena_http 需要代理/
                                # 授权网段/平台地址；平台隧道代理（HTTP(S)_PROXY/
                                # NO_PROXY）供子进程访问赛题靶机；绝不放行
                                # ARENA_SOLVER_TOKEN 等凭据（凭据泄露面必须为零）。
                                env=tool_environment())
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
    """docker 只读沙箱规格：pin 死镜像、禁网络、非特权用户与工作区挂载。"""
    image: str
    workspace: str
    name: str

    def argv(self, command):
        """构造 docker 只读沙箱 argv：镜像必须 pin sha256，禁网络/能力/写根文件系统。"""
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
    """可信命令执行器：仅供受信适配器调用；只允许固定离线解码器，不执行模型代码。"""
    def __init__(self, store, control, environment, *, allow_local_mock=False):
        """allow_local_mock=True 仅在 Mock 协议下启用本地解码器。"""
        self.store, self.control, self.environment = store, control, environment
        self.allow_local_mock = allow_local_mock

    def execute_mock_decoder(self, data, *, decode=True, target_revision='mock-attachment-v1'):
        """固定 base64 解码器（经 stdin）：带缓存、预算预留与命令日志。"""
        # 只允许固定离线解码程序；不执行模型提供的 Python 或 shell。
        import sys
        if not self.allow_local_mock:
            raise PermissionError('local decoder is restricted to mock protocol')
        script = ('import base64,sys;sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read(),validate=True))'
                  if decode else 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())')
        # -S 跳过 site 初始化：调试器注入的 sitecustomize 会把告警混进 stdout，
        # 污染解码结果与证据；固定解码脚本只依赖标准库。
        argv = [sys.executable, '-I', '-S', '-c', script]
        version = 'python-' + sys.version.split()[0]
        input_hash = hashlib.sha256(data).hexdigest()
        cid = self.control.challenge_id
        key = self.store.cache_key(cid, target_revision, self.environment, 'fixture-decoder', version, argv, input_hash)
        self.control.reserve('fixture-decoder', version, argv, input_hash)
        cached = self.store.cached(key)
        if cached:
            return self.store.restore_evidence(cached['evidence_id']), cached['evidence_id'], cached['id'], True
        result = capture(argv, cwd=str(self.store.workspace(cid)),
                         timeout=min(15, self.control.remaining()['seconds']),
                         input_data=data, discard_stderr=True)
        eid = self.store.evidence.put_bytes(result['output'], kind='command_output')
        command_id = 'cmd-' + uuid.uuid4().hex
        complete = result['complete'] and result['returncode'] == 0
        with self.store.board.transaction() as db:
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
