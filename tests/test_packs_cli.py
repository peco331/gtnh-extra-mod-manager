"""CLI 非交互入口（--packs-check / --packs-update）回归。

关键语义：``--packs-update`` 与 mod 的 ``--update-all`` 一致，**只动已安装
条目**。回归背景：曾用全量 ``installable()`` 当安装清单，刷新一次 wiki 后
跑一次 ``--packs-update`` 会把上游所有可自动下载的包全部下载安装。
"""
import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from gtnhmod import cli, packs
from gtnhmod import packs_wiki as W


def make_zip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pack.mcmeta", "{}")
    return path


def fake_entry(pack_id, name, github_url, kind=W.KIND_RESOURCE):
    return {
        "id": pack_id, "kind": kind, "name_en": name, "category": "测试",
        "author": "", "scope": "", "loader": "", "desc": "", "detail": "",
        "version_requirements": [], "icon": None,
        "urls": {"links": [], "github": github_url, "modrinth": None,
                 "curseforge": None, "primary": github_url, "direct": [github_url]},
        "aliases": [], "local": False, "wiki_removed": False,
    }


def ok_result(name, version):
    a = packs.PackAsset(url=f"https://x/{name}", file_name=name,
                        version=version, tag=version, source="github")
    return packs.PackSourceResult(source="github", status="ok", assets=[a],
                                  latest_version=version, url="https://github.com/a/b")


class TestPacksUpdateNonInteractive(unittest.TestCase):
    """--packs-update 只安装/更新已安装条目；未安装条目绝不批量拉取。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        folder = self.root / "resourcepacks"
        make_zip(folder / "Shadow.UI.v5.45.zip")
        self.app = cli.CliApp(self.root / "data")
        self.app.cfg.set_pack_dir(W.KIND_RESOURCE, folder)
        # wiki 数据：A 已装（目录里的文件会被 reconcile 认领），B 未安装
        self.entry_a = fake_entry("pack-shadow", "Shadow UI",
                                  "https://github.com/a/shadow")
        self.entry_b = fake_entry("pack-bsl", "BSL Shaders",
                                  "https://github.com/a/bsl", kind=W.KIND_SHADER)
        self.app.packs.merge_wiki([self.entry_a, self.entry_b])
        packs.reconcile_installed(self.app.cfg, self.app.packs,
                                  kinds=[W.KIND_RESOURCE])
        self.assertIn("pack-shadow", self.app.packs.installed(W.KIND_RESOURCE))

    def test_uninstalled_entries_are_not_mass_installed(self):
        """上游对 A/B 都有可下载文件，也只有已装的 A 被更新。"""
        attempted = []

        def fake_apply(cfg, db, entry, asset, **kw):
            attempted.append(entry["id"])
            return True, "ok"

        results = {"pack-shadow": ok_result("Shadow.UI.v5.46.zip", "v5.46"),
                   "pack-bsl": ok_result("BSL_v10.2.0.zip", "v10.2.0")}

        with mock.patch.object(cli.packmod, "list_assets",
                               side_effect=lambda cfg, e, **kw: results[e["id"]]), \
             mock.patch.object(cli.packmod, "apply_install", side_effect=fake_apply), \
             redirect_stdout(io.StringIO()):
            rc = cli.run_packs_update(self.app)

        self.assertEqual(rc, 0)
        self.assertEqual(attempted, ["pack-shadow"])   # B 未安装，绝不批量安装

    def test_up_to_date_installation_is_skipped(self):
        """已是最新版本时不重装（更新计划为 skip）。"""
        attempted = []
        results = {"pack-shadow": ok_result("Shadow.UI.v5.45.zip", "v5.45"),
                   "pack-bsl": ok_result("BSL_v10.2.0.zip", "v10.2.0")}

        def fake_apply(cfg, db, entry, asset, **kw):
            attempted.append(entry["id"])
            return True, "ok"

        with mock.patch.object(cli.packmod, "list_assets",
                               side_effect=lambda cfg, e, **kw: results[e["id"]]), \
             mock.patch.object(cli.packmod, "apply_install", side_effect=fake_apply), \
             redirect_stdout(io.StringIO()):
            rc = cli.run_packs_update(self.app)

        self.assertEqual(rc, 0)
        self.assertEqual(attempted, [])

    def test_missing_pack_dir_reports_and_skips(self):
        self.app.cfg.set_pack_dir(W.KIND_RESOURCE, "")
        self.app.cfg.set_pack_dir(W.KIND_SHADER, "")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.run_packs_update(self.app)
        self.assertEqual(rc, 0)
        self.assertIn("未设置", buf.getvalue())

    def test_check_reports_skip_reason_in_both_entrypoints(self):
        for detail in ("本地版本更高（v9.0）", "无法比较版本，请手动选择"):
            plan = packs.InstallPlanItem("pack-shadow", "Shadow UI", W.KIND_RESOURCE,
                                         "skip", detail=detail)
            for check in (lambda: cli.run_packs_check(self.app), self.app._packs_check):
                with self.subTest(detail=detail, check=check), \
                     mock.patch.object(cli.packmod, "plan_install", return_value=[plan]), \
                     redirect_stdout(io.StringIO()) as buf:
                    self.assertEqual(check(), 0)
                self.assertIn(detail, buf.getvalue())
                self.assertNotIn("已最新", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
