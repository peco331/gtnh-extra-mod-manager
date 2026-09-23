"""GTNH 平台筛选与发布选择回归；不接触用户目录或外网。"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from gtnhmod import downloader, updater
from gtnhmod.config import Config
from gtnhmod.sources import GitHubSource
from gtnhmod.targets import classify_target


def release(tag, names, date='2026-09-01T00:00:00Z', **extra):
    return dict(tag_name=tag, published_at=date, assets=[
        dict(name=n, browser_download_url='https://example.invalid/' + n)
        for n in names], **extra)


class TestTargets(unittest.TestCase):
    def _options_for_entry(self, entry, data):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(Path(td))
            with patch.object(GitHubSource, "_api", return_value=(data, "fresh")):
                return updater.list_install_options(entry, cfg, force=True)

    def test_wiki_catalog_is_positive_gtnh_source_evidence(self):
        entry = {
            "id": "demo", "name_en": "Demo", "group": "星门规则",
            "source_type": "github",
            "source": {"owner": "owner", "repo": "Demo"},
        }
        options, err = self._options_for_entry(
            entry, [release("4.0", ["Demo-4.0.jar"])])
        self.assertIsNone(err)
        self.assertEqual(options[0]["target_status"], "eligible")

    def test_gtnh_repository_name_is_positive_gtnh_source_evidence(self):
        entry = {
            "id": "ae2wtx", "name_en": "ae2wtx", "group": "自定义",
            "source_type": "github",
            "source": {"owner": "peco331", "repo": "AE2WirelessTransceiver-GTNH"},
        }
        options, err = self._options_for_entry(
            entry, [release("1.1.0", ["ae2wtx-1.1.0.jar"])])
        self.assertIsNone(err)
        self.assertEqual(options[0]["target_status"], "eligible")

    def test_unmarked_custom_repository_still_requires_confirmation(self):
        entry = {
            "id": "demo", "name_en": "Demo", "group": "自定义",
            "source_type": "github",
            "source": {"owner": "owner", "repo": "Demo"},
        }
        options, err = self._options_for_entry(
            entry, [release("4.0", ["Demo-4.0.jar"])])
        self.assertIsNone(err)
        self.assertEqual(options[0]["target_status"], "unknown")

    def test_evidence(self):
        cases = [
            ('Demo-mc1.20.1-4.0.jar', '', 'gtnh', 'excluded'),
            ('Demo-1.20.1-4.0.jar', '', 'unknown', 'excluded'),
            ('Demo-1.7.10-4.0.jar', '', 'unknown', 'eligible'),
            ('Demo-1.20.1.jar', '', 'gtnh', 'eligible'),
            ('Demo-4.0.jar', '', 'unknown', 'unknown'),
            ('Demo-4.0-fabric.jar', '', 'gtnh', 'excluded'),
            ('Demo-4.0-neoforge.jar', '', 'gtnh', 'excluded'),
            ('Demo-1.7.10-forge-4.0.jar', '', 'unknown', 'eligible'),
            ('Demo-4.0.jar', '4.0-GTNH', 'unknown', 'eligible'),
            ('Demo-1.21.1-4.0.jar', '4.0-GTNH', 'gtnh', 'excluded'),
            ('Demo-4.0.jar', '4.0-mc1.21.1', 'gtnh', 'excluded'),
            ('Demo-mc1.7.10-1.20.1.jar', '', 'unknown', 'eligible'),
            ('Demo-mc1.6.4-4.0.jar', '', 'gtnh', 'excluded'),
            ('Demo-mc1.6.4-4.0-2.0.jar', '', 'gtnh', 'excluded'),
            ('Demo-1.7.10-1.12.0.jar', '', 'unknown', 'eligible'),
            ('Demo-1.21-4.0.jar', '', 'gtnh', 'excluded'),
            ('Demo-1.12-4.0.jar', '', 'gtnh', 'excluded'),
        ]
        for name, tag, profile, status in cases:
            with self.subTest(name=name, tag=tag):
                self.assertEqual(classify_target(name, release_tag=tag,
                    target_profile=profile).status, status)

    def test_latest_qualified_prerelease(self):
        data = [release('9.0', ['Demo-1.21.1-9.0.jar']),
                release('2.0-beta1', ['Demo-1.7.10-2.0-beta1.jar'], prerelease=True),
                release('8.0', ['Demo-1.7.10-8.0.jar'], '2026-08-01T00:00:00Z')]
        src = GitHubSource('owner', 'Demo')
        with patch.object(src, '_api', return_value=(data, 'fresh')):
            self.assertEqual(src.check(None).latest_version, '2.0-beta1')
            opts = src.list_versions()
        self.assertEqual([x.version for x in opts], ['2.0-beta1', '8.0'])

    def test_mixed_assets_and_unknown(self):
        src = GitHubSource('owner', 'Demo')
        data = [release('4.0', ['Demo-4.0.jar', 'Demo-1.21.1-4.0.jar',
                              'Demo-1.7.10-4.0.jar', 'Demo-1.7.10-4.0-sources.jar'])]
        with patch.object(src, '_api', return_value=(data, 'fresh')):
            info = src.check(None)
        self.assertEqual([c.file_name for c in info.candidates], ['Demo-1.7.10-4.0.jar'])

    def test_unknown_does_not_claim_latest(self):
        src = GitHubSource('owner', 'Demo')
        with patch.object(src, '_api', return_value=([release('4.0', ['Demo-4.0.jar'])], 'fresh')):
            with self.assertRaisesRegex(Exception, '确认'):
                src.check(None)

    def test_second_page_and_budget(self):
        src = GitHubSource('owner', 'Demo')
        wrong = [release(str(i), [f'Demo-mc1.21.1-{i}.jar']) for i in range(30)]
        good = [release('2.0', ['Demo-1.7.10-2.0.jar'])]
        with patch.object(src, '_api', side_effect=[(wrong, 'fresh'), (good, 'fresh')]) as api:
            self.assertEqual(src.check(None).latest_version, '2.0')
            self.assertEqual(api.call_count, 2)
        with patch.object(src, '_api', return_value=(wrong, 'fresh')) as api:
            with self.assertRaisesRegex(Exception, '范围'):
                src.check(None)
            self.assertEqual(api.call_count, 5)

    def test_budget_does_not_override_an_already_found_release(self):
        src = GitHubSource("owner", "Demo")
        wrong = [release(str(i), [f"Demo-mc1.21.1-{i}.jar"]) for i in range(30)]
        first_page = [release("2.0", ["Demo-1.7.10-2.0.jar"])] + wrong[:29]
        pages = [(first_page, "fresh")] + [(wrong, "fresh") for _ in range(4)]
        with patch.object(src, "_api", side_effect=pages):
            try:
                latest = src.check(None).latest_version
            except Exception as exc:
                self.fail(f"already-found release was overridden by search budget: {exc}")
            self.assertEqual(latest, "2.0")

    def test_check_stops_after_first_page_with_qualified_release(self):
        src = GitHubSource("owner", "Demo")
        wrong = [release(str(i), [f"Demo-mc1.21.1-{i}.jar"]) for i in range(30)]
        first_page = [release("2.0", ["Demo-1.7.10-2.0.jar"])] + wrong[:29]
        pages = [(first_page, "fresh")] + [(wrong, "fresh") for _ in range(4)]
        with patch.object(src, "_api", side_effect=pages) as api:
            self.assertEqual(src.check(None).latest_version, "2.0")
            self.assertEqual(api.call_count, 1)

    def test_unknown_newer_does_not_hide_behind_old(self):
        src = GitHubSource('owner', 'Demo')
        data = [release('4.0', ['Demo-4.0.jar']),
                release('3.0-GTNH', ['Demo-3.0-GTNH.jar'], '2026-08-01T00:00:00Z')]
        with patch.object(src, '_api', return_value=(data, 'fresh')):
            with self.assertRaisesRegex(Exception, '确认'):
                src.check(None)

    def test_compat_description_does_not_block_beta(self):
        option = dict(version='3.0-beta1', compat='incompatible', prerelease=True,
                      target_status='eligible', candidates=[object()])
        chosen, _ = updater._pick_default_option([option])
        self.assertIs(chosen, option)

    def test_profile_binding_lifetime(self):
        from gtnhmod.db import ModsDB
        with tempfile.TemporaryDirectory() as td:
            db = ModsDB(Path(td) / 'db.json')
            mid = db.add_custom(dict(name_en='Demo', source_type='github',
                                    source=dict(owner='owner', repo='Demo')))
            updater.confirm_gtnh_source(db, mid, True)
            self.assertEqual(db.get(mid)['source']['target_profile'], 'gtnh')
            updater.bind_source(db, mid, 'https://github.com/owner/Demo')
            self.assertEqual(db.get(mid)['source']['target_profile'], 'gtnh')
            updater.bind_source(db, mid, 'https://github.com/other/Demo')
            self.assertNotEqual(db.get(mid)['source'].get('target_profile'), 'gtnh')

    def test_local_mixed_platform(self):
        from gtnhmod.sources import LocalFolderSource
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('Demo-1.21.1-9.0.jar', 'Demo-1.7.10-2.0.jar'):
                (root / name).write_bytes(b'placeholder')
            src = LocalFolderSource(str(root))
            self.assertEqual(src.check(None).latest_version, '2.0')
            self.assertEqual([v.version for v in src.list_versions()], ['2.0'])

    def test_confirmed_source_still_excludes_modern(self):
        src = GitHubSource('owner', 'Demo', target_profile='gtnh')
        data = [release('4.0', ['Demo-mc1.21.1-4.0.jar', 'Demo-4.0.jar'])]
        with patch.object(src, '_api', return_value=(data, 'fresh')):
            self.assertEqual([c.file_name for c in src.check(None).candidates], ['Demo-4.0.jar'])

    def test_latest_target_without_asset_remains_manual(self):
        src = GitHubSource('owner', 'Demo')
        data = [release('4.0-GTNH', []),
                release('3.0-GTNH', ['Demo-3.0-GTNH.jar'], '2026-08-01T00:00:00Z')]
        with patch.object(src, '_api', return_value=(data, 'fresh')):
            info = src.check(None)
        self.assertEqual(info.latest_version, '4.0-GTNH')
        self.assertIsNone(info.candidates)

    def test_draft_and_wrong_platform_do_not_shadow_same_version(self):
        src = GitHubSource('owner', 'Demo')
        data = [release('4.0', ['Demo-1.21.1-4.0.jar']),
                release('5.0', ['Demo-1.7.10-5.0.jar'], draft=True),
                release('4.0', ['Demo-1.7.10-4.0.jar'])]
        with patch.object(src, '_api', return_value=(data, 'fresh')):
            self.assertEqual(src.check(None).candidates[0].file_name, 'Demo-1.7.10-4.0.jar')

    def test_invalid_date_reports_fallback(self):
        src = GitHubSource('owner', 'Demo')
        with patch.object(src, '_api', return_value=(
                [release('4.0-GTNH', ['Demo-4.0-GTNH.jar'], 'not-a-date')], 'fresh')):
            self.assertIn('时间', src.check(None).note)

    def test_partial_dates_preserve_upstream_order_with_warning(self):
        src = GitHubSource('owner', 'Demo')
        data = [release('3.0-GTNH', ['Demo-3.0-GTNH.jar'], None),
                release('2.0-GTNH', ['Demo-2.0-GTNH.jar'])]
        with patch.object(src, '_api', return_value=(data, 'fresh')):
            info = src.check(None)
        self.assertEqual(info.latest_version, '3.0-GTNH')
        self.assertIn('时间', info.note)

    def test_wiki_merge_preserves_only_same_source_confirmation(self):
        from gtnhmod.db import ModsDB
        from copy import deepcopy
        fresh = dict(id='demo', name_en='Demo', group='星门规则', category='功能增强',
                     source_type='github', source=dict(owner='owner', repo='Demo'))
        with tempfile.TemporaryDirectory() as td:
            db = ModsDB(Path(td) / 'db.json')
            db.merge_wiki([deepcopy(fresh)])
            updater.confirm_gtnh_source(db, 'demo', True)
            db.merge_wiki([deepcopy(fresh)])
            self.assertEqual(db.get('demo')['source']['target_profile'], 'gtnh')
            fresh['source']['repo'] = 'Other'
            db.merge_wiki([deepcopy(fresh)])
            self.assertNotEqual(db.get('demo')['source'].get('target_profile'), 'gtnh')


class TestJarTarget(unittest.TestCase):
    def test_install_and_update_cannot_bypass_target_gate(self):
        from gtnhmod.config import Config
        from gtnhmod.db import ModsDB
        from gtnhmod.installed import InstalledDB
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mods = root / 'mods'
            mods.mkdir()
            old = mods / 'Demo-1.7.10-1.0.jar'
            old.write_bytes(b'old-file')
            cfg = Config(root)
            cfg.set_mods_dir('client', mods)
            db, inst = ModsDB(root / 'db.json'), InstalledDB(root / 'installed.json')
            mid = db.add_custom(dict(name_en='Demo', source_type='manual'))
            option = dict(version='2.0', compat='unknown', body='', candidates=[object()],
                          target_status='excluded', target_reason='不适用于 GTNH')
            for action in (updater.install_mod, updater.update_mod):
                for version in (None, '2.0'):
                    with self.subTest(action=action.__name__, version=version), \
                            patch.object(downloader, 'update_with_backup') as download:
                        result = action(cfg, db, inst, mid, 'client', version=version,
                                        prefetched=([option], None))
                        self.assertEqual(result['action'], 'manual')
                        download.assert_not_called()
                        self.assertEqual(old.read_bytes(), b'old-file')
            option.update(version='0.9', target_status='eligible')
            with patch.object(downloader, 'update_with_backup') as download:
                result = updater.update_mod(cfg, db, inst, mid, 'client',
                                            prefetched=([option], None))
                self.assertEqual(result['action'], 'manual')
                self.assertIn('降级', result['note'])
                download.assert_not_called()

    def test_metadata_before_replacement(self):
        for entries in ({'fabric.mod.json': '{}'},
                        {'META-INF/neoforge.mods.toml': ''},
                        {'mcmod.info': json.dumps([{'mcversion': '1.20.1'}])}):
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                incoming = root / 'incoming.jar'
                with zipfile.ZipFile(incoming, 'w') as z:
                    for name, data in entries.items():
                        z.writestr(name, data)
                old = root / 'mods' / 'Demo.jar'
                old.parent.mkdir()
                old.write_bytes(b'old-file')
                from gtnhmod.sources import DownloadCandidate
                with self.assertRaises(downloader.VerifyError):
                    downloader.update_with_backup(
                        DownloadCandidate(str(incoming), 'Demo.jar'), old.parent,
                        root / 'backup', old_file=old)
                self.assertEqual(old.read_bytes(), b'old-file')
                self.assertFalse((root / 'backup').exists())

    def test_missing_metadata_is_not_incompatible(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'Demo.jar'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('Demo.class', b'dummy')
            downloader.verify_target_jar(path)
