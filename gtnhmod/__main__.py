"""入口：py -m gtnhmod [cli|gui] [--check|--update-all]"""

import sys


def main():
    args = sys.argv[1:]
    if "gui" in args:
        from . import gui
        gui.run()
    else:
        from . import cli
        cli.run(args)


if __name__ == "__main__":
    main()
