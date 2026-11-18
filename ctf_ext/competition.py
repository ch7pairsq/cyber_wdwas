"""参赛候选入口：规范未到齐前明确拒绝启动，不安装包、不调用模型、不回退 Mock。"""
import sys


def main():
    print("参赛启动已阻止：官方 Harness 第 2.2 节协议与入口未接通；请勿提交此候选镜像。", file=sys.stderr)
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
