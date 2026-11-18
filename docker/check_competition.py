"""只读离线检查参赛候选镜像，不访问网络、不安装依赖。"""
import importlib.util
import json
import shutil
import sys
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_toolchain import load_manifest, verify_tools

for module in ('pip', 'ensurepip', 'uv', 'debugpy', 'pytest'):
    assert importlib.util.find_spec(module) is None, '禁止的模块仍存在：' + module
for command in ('pip', 'pip3', 'uv', 'uvx', 'apt', 'apt-get', 'dpkg'):
    assert shutil.which(command) is None, '安装器仍存在：' + command
for module in ('ctf_ext', 'cairn', 'fastapi', 'uvicorn', 'requests', 'yaml', 'docker'):
    assert importlib.util.find_spec(module) is not None, '缺少预装依赖：' + module
from ctf_ext.llm.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL
assert DEFAULT_BASE_URL == 'https://ai-api-gateway.app.baizhi.cloud/api/anthropic'
assert DEFAULT_MODEL == 'deepseek-flash'
assert not Path('/var/run/docker.sock').exists()
assert not Path('/app/.env').exists()

metadata_path = Path('/opt/toolchain-installed.json')
assert metadata_path.is_file(), '缺少工具链安装清单：' + str(metadata_path)
metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
tier = metadata.get('tier', 'ctf')
missing = verify_tools(load_manifest(Path('/app/docker/toolchain.json')), tier)
assert not missing, '工具链缺失：' + ', '.join(missing)
print(json.dumps({'offline_check': 'passed', 'cairn': version('cairn'), 'model': DEFAULT_MODEL,
                  'toolchain_tier': tier, 'toolchain_tools': len(metadata.get('tools', {})),
                  'runtime_installers': False, 'harness_ready': False}, ensure_ascii=False))
