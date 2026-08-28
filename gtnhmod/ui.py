"""UI 抽象：核心逻辑与壳之间的交互接口。

核心库不直接 input/print，所有交互经 UIProtocol，CLI/GUI/测试各自实现。
"""
import sys


class UIProtocol:
    def choose(self, title: str, options: list, allow_cancel: bool = True) -> int | None:
        """单选菜单，返回下标；None=取消。"""
        raise NotImplementedError

    def confirm(self, msg: str) -> bool:
        raise NotImplementedError

    def info(self, msg: str) -> None:
        raise NotImplementedError

    def warn(self, msg: str) -> None:
        raise NotImplementedError

    def error(self, msg: str) -> None:
        raise NotImplementedError

    def input_text(self, prompt: str, default: str = "") -> str:
        raise NotImplementedError


class ConsoleUI(UIProtocol):
    """控制台交互（ANSI 色，非 tty 时自动关闭）。"""

    RED, GREEN, YELLOW, CYAN, DIM, RESET = (
        "\033[91m", "\033[92m", "\033[93m", "\033[96m", "\033[2m", "\033[0m")

    def __init__(self, use_color: bool = None):
        self.use_color = sys.stdout.isatty() if use_color is None else use_color

    def _c(self, s: str, color: str) -> str:
        return f"{color}{s}{self.RESET}" if self.use_color else s

    def info(self, msg):
        print(msg)

    def warn(self, msg):
        print(self._c(f"[!] {msg}", self.YELLOW))

    def error(self, msg):
        print(self._c(f"[错误] {msg}", self.RED))

    def ok(self, msg):
        print(self._c(msg, self.GREEN))

    def choose(self, title, options, allow_cancel=True):
        print(title)
        for i, opt in enumerate(options, 1):
            print(f"  {self._c(str(i), self.CYAN)}. {opt}")
        if allow_cancel:
            print(f"  {self._c('0', self.CYAN)}. 取消/返回")
        while True:
            try:
                s = input("请选择: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if allow_cancel and s == "0":
                return None
            try:
                idx = int(s) - 1
                if 0 <= idx < len(options):
                    return idx
            except ValueError:
                pass
            print("无效选择，请重新输入")

    def confirm(self, msg):
        while True:
            try:
                s = input(f"{msg} [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if s in ("y", "yes", "是"):
                return True
            if s in ("", "n", "no", "否"):
                return False

    def input_text(self, prompt, default=""):
        suffix = f" [{default}]" if default else ""
        try:
            s = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return default
        return s or default
