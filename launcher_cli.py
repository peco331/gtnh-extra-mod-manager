"""PyInstaller CLI 入口：打包为 gtnh-cli.exe（console）。"""
import sys

from gtnhmod.cli import run

sys.exit(run(sys.argv[1:]) or 0)
