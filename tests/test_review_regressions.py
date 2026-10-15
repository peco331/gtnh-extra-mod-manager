"""完整审查中发现的问题：隔离目录、真实文件替换与模拟上游。"""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from gtnhmod import downloader, gui, net, packs, scanner, updater, versions, wiki
from gtnhmod.config import Config
from gtnhmod.db import ModsDB
from gtnhmod.installed import InstalledDB
from gtnhmod.sources import DownloadCandidate, GitHubSource, SourceError


def jar(path, text='hello'):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('test.txt', text)


def packzip(path, text='hello'):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('pack.mcmeta', text)


class ReviewFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gtnh_review_')
        self.root = Path(self.temp.name)
        self.cfg = Config(self.root / 'data')
        self.mods = self.root / 'mods'
        self.mods.mkdir()
        self.cfg.data['mods_folders']['client'] = str(self.mods)
        self.db = ModsDB(self.root / 'data' / 'mods_db.json')
        self.inst = InstalledDB(self.root / 'data' / 'installed.json')
        self.mid = self.db.add_custom({'name_en': 'Demo', 'source_type': 'github',
                                      'source': {'owner': 'GTNewHorizons', 'repo': 'Demo'}})

    def tearDown(self):
        self.temp.cleanup()

    def options(self):
        return [{'version': '2.0', 'tag': '2.0', 'target_status': 'eligible',
                 'target_reason': '', 'body': '', 'prerelease': False,
                 'published_at': '2026-10-01T00:00:00Z',
                 'candidates': [DownloadCandidate('https://example.invalid/demo.jar',
                                                  'Demo-2.0.jar')]}]

    def test_wtct_installed_version_keeps_rc_suffix(self):
        self.assertEqual(versions.split_mc_mod_version('wtct-1.0.63-rc1'),
                         ('wtct', None, '1.0.63-rc1'))
        self.assertEqual(versions.split_mc_mod_version('Demo-1.12.2-beta1'),
                         ('Demo', None, '1.12.2-beta1'))
        self.assertGreater(versions.compare('1.12.2', '1.12.2-beta1'), 0)

    def test_zero_cache_interval_is_honored_and_nonfinite_rejected(self):
        self.cfg.data['check_interval_hours'] = 0
        self.assertEqual(self.cfg.check_interval_hours, 0)
        self.cfg.data['check_interval_hours'] = float('nan')
        self.assertEqual(self.cfg.check_interval_hours, 6)

    def test_target_collision_does_not_overwrite_other_file(self):
        source = self.root / 'download.jar'
        jar(source)
        old, target = self.mods / 'Demo-1.0.jar', self.mods / 'Demo-2.0.jar'
        old.write_bytes(b'old')
        target.write_bytes(b'unrelated')
        with self.assertRaises(downloader.VerifyError):
            downloader.update_with_backup(DownloadCandidate(str(source), target.name),
                self.mods, self.cfg.backup_dir, old_file=old)
        self.assertEqual(old.read_bytes(), b'old')
        self.assertEqual(target.read_bytes(), b'unrelated')

    def test_windows_device_and_stream_names_rejected(self):
        for name in ('NUL.jar', 'CON.jar', 'Demo:payload.jar', 'a?.jar'):
            with self.subTest(name=name), self.assertRaises(downloader.VerifyError):
                downloader._validate_candidate(DownloadCandidate('unused', name))

    def test_backup_failure_cleans_staging_and_keeps_old_file(self):
        source = self.root / 'download.jar'
        jar(source)
        old = self.mods / 'Demo-1.0.jar'
        old.write_bytes(b'old')
        with patch.object(downloader.shutil, 'copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                downloader.update_with_backup(DownloadCandidate(str(source), 'Demo-2.0.jar'),
                    self.mods, self.cfg.backup_dir, old_file=old)
        self.assertEqual(old.read_bytes(), b'old')
        self.assertFalse(list(self.mods.glob('.dl_*')))

    def test_restore_disabled_mod_preserves_disabled_state(self):
        folder = self.cfg.backup_dir / 'client' / self.mid
        folder.mkdir(parents=True)
        source = folder / '20261015_010101_Demo-1.0.jar.disabled'
        jar(source)
        result = updater.restore_backup(self.cfg, self.db, self.inst, 'client', source)
        self.assertEqual(result['action'], 'restored', result)
        self.assertTrue((self.mods / 'Demo-1.0.jar.disabled').exists())
        self.assertFalse(self.inst.get('client', self.mid)['enabled'])
        self.assertEqual(self.inst.get('client', self.mid)['parsed_version'], '1.0')

    def test_rollback_snapshot_survives_first_side_backup_rotation(self):
        server = self.root / 'server'
        server.mkdir()
        self.cfg.data['mods_folders']['server'] = str(server)
        self.cfg.data['backup_keep'] = 1
        for folder in (self.mods, server):
            jar(folder / 'Demo-2.0.jar')
        backup_folder = self.cfg.backup_dir / 'client' / self.mid
        backup_folder.mkdir(parents=True)
        jar(backup_folder / '20261001_010101_Demo-1.0.jar')
        results = updater.rollback_mod(self.cfg, self.db, self.inst, self.mid)
        self.assertEqual([r['action'] for r in results], ['restored', 'restored'])
        self.assertTrue(all((p / 'Demo-1.0.jar').exists() for p in (self.mods, server)))

    def test_repeated_delete_in_same_second_preserves_both_copies(self):
        with patch.object(updater.utils, 'timestamp_str', return_value='20261015_010101'):
            for content in ('first', 'second'):
                jar(self.mods / 'Demo-1.0.jar', content)
                self.assertEqual(updater.delete_mod(self.cfg, self.db, self.inst, self.mid)['action'], 'deleted')
        copies = list((self.cfg.backup_dir / 'client' / self.mid).glob('*.deleted'))
        self.assertEqual(len(copies), 2)
        self.assertNotEqual(copies[0].read_bytes(), copies[1].read_bytes())

    def test_failed_scan_preserves_installed_records(self):
        self.inst.set('client', self.mid, file_name='Demo-1.0.jar', locked=True)
        with patch.object(scanner, 'scan_folder', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                updater.reconcile_installed(self.cfg, self.db, self.inst)
        self.assertTrue(self.inst.get('client', self.mid)['locked'])

    def test_equal_mod_matches_are_not_guessed(self):
        file = scanner.InstalledFile(self.mods / 'Shared-1.0.jar', 'Shared-1.0.jar',
                                     True, 'Shared', None, '1.0')
        entries = [{'id': 'one', 'name_en': 'One', 'aliases': ['Shared']},
                   {'id': 'two', 'name_en': 'Two', 'aliases': ['Shared']}]
        self.assertEqual(scanner.match_db(file, entries), (None, 'ambiguous'))

    def test_automatic_duplicate_cleanup_keeps_fuzzy_addon_match(self):
        new = self.mods / 'Demo-2.0.jar'
        addon = self.mods / 'DemoAddon-1.0.jar'
        old = self.mods / 'Demo-1.0.jar'
        for path in (new, addon, old):
            jar(path)
        updater._cleanup_extras(self.cfg, self.db, 'client', self.mid, new)
        self.assertTrue(addon.exists())
        self.assertFalse(old.exists())
        self.assertEqual(len(list((self.cfg.backup_dir / 'client' / self.mid).glob('*.jar'))), 1)

    def test_pack_query_failure_does_not_cancel_other_entries(self):
        entries = [{'id': 'bad', 'name_en': 'Bad', 'kind': 'resourcepack'},
                   {'id': 'ok', 'name_en': 'Good', 'kind': 'resourcepack'}]
        db = packs.PacksDB(self.root / 'packs.json')
        responses = [RuntimeError('timeout'),
                     packs.PackSourceResult(source='manual', status='unsupported', note='需手动')]
        with patch.object(packs, 'list_assets', side_effect=responses):
            plans = packs.plan_install(self.cfg, db, entries)
        self.assertEqual([(p.pack_id, p.action) for p in plans], [('bad', 'error'), ('ok', 'manual')])

    def test_mod_asset_ambiguity_requires_choice_but_keeps_platform_evidence(self):
        release = {'tag_name': '2.0', 'assets': [
            {'name': n, 'browser_download_url': 'https://example.invalid/' + n}
            for n in ('Demo-client-2.0.jar', 'Demo-server-2.0.jar')]}
        src = GitHubSource('GTNewHorizons', 'Demo')
        with patch.object(src, '_api', return_value=([release], 'fresh')):
            option = src.list_versions()[0]
            info = src.check(None)
        self.assertEqual(option.target_status, 'eligible')
        self.assertTrue(option.asset_ambiguous)
        self.assertEqual(len(option.candidates), 2)
        self.assertIsNone(info.candidates)

    def test_github_url_parser_requires_actual_host_and_strips_git_suffix(self):
        self.assertIsNone(wiki.github_repo_from_url('https://evil.invalid/github.com/owner/repo'))
        self.assertIsNone(wiki.github_repo_from_url('https://github.com.evil.invalid/owner/repo'))
        self.assertEqual(wiki.github_repo_from_url('https://github.com/owner/repo.git'), ('owner', 'repo'))

    def test_automatic_update_unknown_local_version_requires_choice(self):
        jar(self.mods / 'Demo.jar')
        with patch.object(updater, 'list_install_options', return_value=(self.options(), None)), \
                patch.object(downloader, 'update_with_backup') as download:
            result = updater.update_mod(self.cfg, self.db, self.inst, self.mid, 'client')
        self.assertEqual(result['action'], 'manual')
        self.assertIn('未知', result['note'])
        download.assert_not_called()

    def test_dual_side_plan_compares_each_side(self):
        reg = {s: {self.mid: dict(enabled=True, locked=False, name_en='Demo', version=v)}
               for s, v in (('client', '2.0'), ('server', '1.0'))}
        with patch.object(updater, 'list_install_options', return_value=(self.options(), None)) as query:
            plan = updater.build_update_plan(self.cfg, self.db, self.inst, registry=reg)
        self.assertEqual({(p['sides'][0], p['action']) for p in plan},
                         {('client', 'uptodate'), ('server', 'update')})
        self.assertEqual(query.call_count, 1)
        self.assertEqual(len(query.call_args.args), 2)  # 查询线程不修改 DB

    def test_empty_local_registry_merges_new_remote_installation(self):
        first = InstalledDB(self.inst.path)
        first.set('client', 'remote', file_name='Remote.jar')
        self.inst.save()
        self.assertEqual(self.inst.get('client', 'remote')['file_name'], 'Remote.jar')
        self.assertEqual(InstalledDB(self.inst.path).get('client', 'remote')['file_name'], 'Remote.jar')

    def test_stale_github_cache_is_not_reported_as_current(self):
        src = GitHubSource('GTNewHorizons', 'Demo', cache_dir=self.cfg.cache_dir)
        with patch.object(net, 'http_get_cached', return_value=([], 'cache_stale')):
            with self.assertRaisesRegex(SourceError, '过期缓存'):
                src.list_versions()

    def test_pack_binding_does_not_fall_back_to_old_release(self):
        assets = [packs.PackAsset('https://example.invalid/new.zip', 'New-2.0.zip',
                                 tag='2.0', published_at='2026-10-01'),
                  packs.PackAsset('https://example.invalid/old.zip', 'Old-1.0.zip',
                                 tag='1.0', published_at='2026-09-01')]
        self.assertIsNone(packs._pick_asset(assets, {'name_en': 'Old'}, asset_regex='^Old'))

    def test_pack_stale_cache_and_malformed_response_are_errors(self):
        for data, origin in (([], 'cache_stale'), ({'message': 'bad'}, 'fresh'),
                             ([None], 'fresh')):
            with self.subTest(data=data, origin=origin), \
                    patch.object(net, 'http_get_cached', return_value=(data, origin)):
                result = packs._github_assets(self.cfg, 'https://github.com/owner/Demo')
                self.assertEqual(result.status, 'error')

    def test_pack_crc_corruption_rejected(self):
        source = self.root / 'corrupt.zip'
        packzip(source)
        source.write_bytes(source.read_bytes().replace(b'hello', b'jello'))
        self.assertFalse(packs.validate_pack(source, 'resourcepack')[0])

    def test_pack_rebinding_clears_old_asset_filter(self):
        db = packs.PacksDB(self.root / 'packs_db.json')
        pid = db.add_custom({'name_en': 'Demo', 'kind': 'resourcepack'})
        packs.set_download_source(db, pid, 'owner/old', asset_regex='Old')
        packs.set_download_source(db, pid, 'owner/new')
        self.assertFalse(db.get(pid).get('asset_regex'))

    def test_pack_restore_retains_existing_file_if_copy_fails(self):
        folder = self.root / 'resourcepacks'
        folder.mkdir()
        dest = folder / 'Demo.zip'
        dest.write_bytes(b'current')
        source = self.root / 'backup.zip'
        source.write_bytes(b'backup')
        record = {'path': source, 'file_name': 'Demo.zip',
                  'kind': 'resourcepacks', 'pack_id': 'demo'}
        with patch.object(packs.shutil, 'copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                packs.restore_backup(self.cfg, record, folder)
        self.assertEqual(dest.read_bytes(), b'current')

    def test_wiki_link_names_are_readable_and_ids_stable(self):
        raw = '[[GTNL:GT-Not-Leisure|GT Not Leisure]]'
        params = {'模组英文名': raw, '模组中文名': '[[中文页|中文名]]'}
        entry = wiki._entry_from_params(params, '非星门规则', '非星门规则')
        self.assertEqual(entry['name_en'], 'GT Not Leisure')
        self.assertEqual(entry['name_cn'], '中文名')
        self.assertEqual(entry['id'], wiki.make_id(raw, '[[中文页|中文名]]'))


if __name__ == '__main__':
    unittest.main()
