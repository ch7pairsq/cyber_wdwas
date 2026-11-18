"""仅在镜像构建阶段移除已知安装器；不是任意 Python 代码的安全沙箱。"""
from pathlib import Path
import shutil

for lib in (Path('/opt/venv/lib/python3.12'), Path('/usr/local/lib/python3.12')):
    for name in ('ensurepip',):
        path = lib / name
        if path.is_dir():
            shutil.rmtree(path)
    site = lib / 'site-packages'
    for pattern in ('pip', 'pip-*.dist-info', 'setuptools', 'setuptools-*.dist-info', '_distutils_hack', 'distutils-precedence.pth'):
        for path in site.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
for directory in ('/opt/venv/bin', '/usr/local/bin', '/usr/bin', '/bin'):
    for name in ('pip', 'pip3', 'pip3.12', 'uv', 'uvx', 'apt', 'apt-get', 'apt-cache', 'apt-config',
                 'apt-mark', 'apt-key', 'dpkg', 'dpkg-deb', 'dpkg-query'):
        path = Path(directory) / name
        if path.is_file() or path.is_symlink():
            path.unlink()
for path in (Path('/uv'), Path('/uvx')):
    if path.is_file() or path.is_symlink():
        path.unlink()
for path in (Path('/app/tests'), Path('/app/cairn/cairn/tests')):
    if path.is_dir():
        shutil.rmtree(path)
for name in ('debug_smoke.py', 'container_smoke.py', 'ci.sh', 'run_checks.py'):
    path = Path('/app/scripts') / name
    if path.exists():
        path.unlink()
