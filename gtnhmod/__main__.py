"""入口：py -m gtnhmod [cli|gui] [--check|--update-all]"""

import sys


def main():
    args = sys.argv[1:]
    if "gui" in args:
        from . import gui
        gui.run()
    else:
        from . import cli
        rc = cli.run(args) or 0
        sys.exit(rc)  # 非交互模式失败时返回非0，任务计划才能感知


if __name__ == "__main__":
    main()
