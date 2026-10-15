"""资源包四页管理的文件安全、锁定与本次检查契约；仅使用临时文件。"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from PIL import Image
from gtnhmod import mod_icons, packs, packs_browser
from gtnhmod.config import Config
from tests.test_packs_manager import fake_entry, make_zip


class TestPackManagement(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(self.root / 'data')
        self.folder = self.root / 'resourcepacks'
        self.folder.mkdir()
        self.cfg.data['pack_folders']['resourcepack'] = str(self.folder)
        self.db = packs.PacksDB(self.cfg.data_dir / 'packs_db.json')
        self.entry = fake_entry()
        self.db.packs = [self.entry, fake_entry('pack-empty', name='Empty')]
        self.file = make_zip(self.folder / 'Demo.Pack.v1.0.zip', ['pack.mcmeta'])
        packs.reconcile_installed(self.cfg, self.db)

    def snapshot(self):
        asset = packs.PackAsset('https://example.invalid/Demo.Pack.v2.0.zip',
                                 'Demo.Pack.v2.0.zip', version='2.0', source='github')
        result = packs.PackSourceResult('github', 'ok', [asset])
        with mock.patch.object(packs, 'list_assets', return_value=result) as query:
            snapshot = packs.check_update_plan(self.cfg, self.db)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(snapshot['plan'][0].action, 'update')
        return snapshot

    def test_disable_enable_keeps_version_lock_and_installed_record(self):
        packs.set_locked(self.db, 'resourcepack', 'pack-demo', True)
        packs.set_enabled(self.cfg, self.db, 'resourcepack', 'pack-demo', False)
        disabled = self.file.with_name(self.file.name + '.disabled')
        self.assertTrue(disabled.exists())
        self.assertFalse(self.file.exists())
        packs.reconcile_installed(self.cfg, self.db)
        rec = self.db.installed('resourcepack')['pack-demo']
        self.assertFalse(rec['enabled'])
        self.assertTrue(rec['locked'])
        self.assertEqual(rec['version'], 'v1.0')
        packs.set_enabled(self.cfg, self.db, 'resourcepack', 'pack-demo', True)
        self.assertTrue(self.file.exists())
        self.assertFalse(disabled.exists())

    def test_disable_rolls_back_if_database_save_fails(self):
        before = dict(self.db.installed('resourcepack')['pack-demo'])
        with mock.patch.object(self.db, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                packs.set_enabled(self.cfg, self.db, 'resourcepack', 'pack-demo', False)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.db.installed('resourcepack')['pack-demo'], before)

    def test_locked_and_disabled_entries_are_not_queried(self):
        for flag in ('locked', 'disabled'):
            if flag == 'locked':
                packs.set_locked(self.db, 'resourcepack', 'pack-demo', True)
            else:
                packs.set_locked(self.db, 'resourcepack', 'pack-demo', False)
                packs.set_enabled(self.cfg, self.db, 'resourcepack', 'pack-demo', False)
            with self.subTest(flag=flag), mock.patch.object(packs, 'list_assets') as query:
                snapshot = packs.check_update_plan(self.cfg, self.db)
                query.assert_not_called()
                self.assertEqual(snapshot['plan'][0].action, 'skip')

    def test_uninstall_preserves_backup_and_unrelated_files(self):
        neighbour = make_zip(self.folder / 'Unrelated.zip', ['pack.mcmeta'])
        data = self.file.read_bytes()
        backups = packs.uninstall_pack(self.cfg, self.db, 'resourcepack', 'pack-demo')
        self.assertFalse(self.file.exists())
        self.assertTrue(neighbour.exists())
        self.assertEqual(backups[0].read_bytes(), data)
        self.assertNotIn('pack-demo', self.db.installed('resourcepack'))

    def test_failed_backup_never_removes_installed_file(self):
        with mock.patch.object(packs, 'backup_existing', return_value=None):
            with self.assertRaises(packs.PacksError):
                packs.uninstall_pack(self.cfg, self.db, 'resourcepack', 'pack-demo')
        self.assertTrue(self.file.exists())

    def test_duplicates_require_manual_updates_and_uninstall_retains_each_backup(self):
        self.cfg.data['backup_keep'] = 1
        second = make_zip(self.folder / 'Demo.Pack.v0.9.zip', ['pack.mcmeta', 'assets/'])
        packs.reconcile_installed(self.cfg, self.db)
        with mock.patch.object(packs, 'list_assets') as query:
            plan = packs.plan_install(self.cfg, self.db, [self.entry])[0]
        query.assert_not_called()
        self.assertEqual(plan.action, 'manual')
        backups = packs.uninstall_pack(self.cfg, self.db, 'resourcepack', 'pack-demo')
        self.assertEqual(len(backups), 2)
        self.assertTrue(all(p.exists() for p in backups))
        self.assertFalse(second.exists())
        self.assertFalse(self.file.exists())

    def test_unpackaged_directory_can_be_disabled_backed_up_and_removed(self):
        self.file.unlink()
        folder = self.folder / 'Demo Pack'
        folder.mkdir()
        (folder / 'pack.mcmeta').write_text('{}', encoding='utf-8')
        packs.reconcile_installed(self.cfg, self.db)
        packs.set_enabled(self.cfg, self.db, 'resourcepack', 'pack-demo', False)
        packs.reconcile_installed(self.cfg, self.db)
        backups = packs.uninstall_pack(self.cfg, self.db, 'resourcepack', 'pack-demo')
        self.assertTrue((backups[0] / 'pack.mcmeta').exists())
        self.assertFalse(folder.with_name(folder.name + '.disabled').exists())

    def test_unsafe_record_cannot_escape_install_directory(self):
        outside = self.root / 'outside.zip'
        outside.write_bytes(b'keep')
        self.db.set_installed('resourcepack', 'pack-demo', {'file_name': '../outside.zip'})
        for operation in (lambda: packs.set_enabled(self.cfg, self.db, 'resourcepack', 'pack-demo', False),
                          lambda: packs.uninstall_pack(self.cfg, self.db, 'resourcepack', 'pack-demo')):
            with self.assertRaises(packs.PacksError):
                operation()
        self.assertEqual(outside.read_bytes(), b'keep')

    def test_link_target_is_rejected_before_backup_or_rename(self):
        with mock.patch.object(Path, 'is_symlink', return_value=True), \
             mock.patch.object(packs, 'backup_existing') as backup:
            with self.assertRaises(packs.PacksError):
                packs.uninstall_pack(self.cfg, self.db, 'resourcepack', 'pack-demo')
        backup.assert_not_called()
        self.assertTrue(self.file.exists())

    def test_directory_unavailable_preserves_record_and_does_not_query_upstream(self):
        before = self.db.installed('resourcepack')
        self.cfg.data['pack_folders']['resourcepack'] = str(self.root / 'not-mounted')
        with mock.patch.object(packs, 'list_assets') as query:
            snapshot = packs.check_update_plan(self.cfg, self.db)
        query.assert_not_called()
        self.assertEqual(self.db.installed('resourcepack'), before)
        self.assertEqual(snapshot['plan'][0].action, 'error')

    def test_checked_plan_does_not_query_again_or_include_uninstalled_entries(self):
        snapshot = self.snapshot()
        self.assertEqual([p.pack_id for p in snapshot['plan']], ['pack-demo'])
        self.assertIsNotNone(snapshot['plan'][0].asset)
        with mock.patch.object(packs, 'list_assets', side_effect=AssertionError('must not query again')):
            self.assertIsNone(packs.validate_checked_updates(self.cfg, self.db, snapshot, snapshot['plan']))

    def test_checked_plan_does_not_expire_but_rejects_changed_file_source_lock_or_directory(self):
        snapshot = self.snapshot()
        snapshot['checked_at'] -= 6 * 3600  # 时间流逝本身不让结果失效
        self.assertIsNone(packs.validate_checked_updates(self.cfg, self.db, snapshot, snapshot['plan']))
        mutations = [lambda: self.db.set_installed('resourcepack', 'pack-demo', {'locked': True}),
                     lambda: self.entry.update(bound_source='https://github.com/new/source'),
                     lambda: self.file.write_bytes(b'changed'),
                     lambda: self.cfg.data['pack_folders'].update(resourcepack=str(self.root / 'gone'))]
        for mutate in mutations:
            # 用独立对象恢复同一个临时夹具，逐个证明失效原因。
            self.cfg.data['pack_folders']['resourcepack'] = str(self.folder)
            self.db.set_installed('resourcepack', 'pack-demo', {'locked': False})
            self.entry.pop('bound_source', None)
            make_zip(self.file, ['pack.mcmeta'])
            snapshot = self.snapshot()
            mutate()
            self.assertIsNotNone(packs.validate_checked_updates(self.cfg, self.db, snapshot, snapshot['plan']))

    def test_restore_replaces_different_version_and_backs_up_current(self):
        original = self.file.read_bytes()
        saved = packs.backup_existing(self.cfg, 'resourcepack', 'pack-demo', self.file)
        self.file.unlink()
        current = make_zip(self.folder / 'Demo.Pack.v2.0.zip', ['pack.mcmeta', 'assets/'])
        packs.reconcile_installed(self.cfg, self.db)
        record = next(r for r in packs.list_backups(self.cfg) if r['path'] == saved)
        restored = packs.restore_installed_backup(self.cfg, self.db, record)
        self.assertEqual(restored.read_bytes(), original)
        self.assertFalse(current.exists())
        self.assertEqual(self.db.installed('resourcepack')['pack-demo']['version'], 'v1.0')
        self.assertTrue(any(r['file_name'].startswith('Demo.Pack.v2.0') for r in packs.list_backups(self.cfg)))

    def test_same_name_restore_clears_unverifiable_upstream_version(self):
        self.file.unlink()
        current = make_zip(self.folder / 'Demo Pack.zip', ['pack.mcmeta'])
        packs.reconcile_installed(self.cfg, self.db)
        saved = packs.backup_existing(self.cfg, 'resourcepack', 'pack-demo', current)
        self.db.set_installed('resourcepack', 'pack-demo', {'version': '9.9', 'locked': True})
        record = next(r for r in packs.list_backups(self.cfg) if r['path'] == saved)
        packs.restore_installed_backup(self.cfg, self.db, record)
        self.assertIsNone(self.db.installed('resourcepack')['pack-demo']['version'])
        self.assertTrue(self.db.installed('resourcepack')['pack-demo']['locked'])

    def test_disabled_zip_backup_collision_restores_original_name_and_state(self):
        packs.set_enabled(self.cfg, self.db, 'resourcepack', 'pack-demo', False)
        disabled = self.file.with_name(self.file.name + '.disabled')
        packs.backup_existing(self.cfg, 'resourcepack', 'pack-demo', disabled)
        saved = packs.backup_existing(self.cfg, 'resourcepack', 'pack-demo', disabled)
        disabled.unlink()
        record = next(r for r in packs.list_backups(self.cfg) if r['path'] == saved)
        dest = packs.restore_installed_backup(self.cfg, self.db, record)
        self.assertEqual(dest.name, disabled.name)
        self.assertFalse(self.db.installed('resourcepack')['pack-demo']['enabled'])
        scanned = packs.scan_pack_dir(self.folder, 'resourcepack')[0]
        self.assertEqual(scanned.ext, '.zip')

    def test_unrelated_file_record_is_never_deleted(self):
        note = self.folder / 'notes.txt'
        note.write_bytes(b'keep')
        self.db.set_installed('resourcepack', 'pack-demo', {'file_name': note.name})
        with self.assertRaises(packs.PacksError):
            packs.uninstall_pack(self.cfg, self.db, 'resourcepack', 'pack-demo')
        self.assertEqual(note.read_bytes(), b'keep')

    def test_association_alias_never_claims_an_unrelated_pack_with_the_same_version(self):
        entry = fake_entry('shader-bsl', kind='shader', name='BSL Shaders', aliases=['BSL_v1.0'])
        known = packs.PackFile(self.root / 'BSL_v1.0.zip', 'BSL_v1.0.zip', False)
        other = packs.PackFile(self.root / 'Unknown.Shader.v1.0.zip', 'Unknown.Shader.v1.0.zip', False)
        self.assertEqual(packs.match_pack(known, [entry])[0], 'shader-bsl')
        self.assertIsNone(packs.match_pack(other, [entry])[0])

    def test_zip_and_directory_pack_icons_load_without_extraction(self):
        from tests.test_mod_icons import picture
        folder = self.root / 'Unpacked'
        folder.mkdir()
        icon = folder / 'pack.png'
        icon.write_bytes(picture())
        import zipfile
        with zipfile.ZipFile(self.file, 'a') as archive:
            archive.writestr('pack.png', picture())
        self.assertEqual(Image.open(io.BytesIO(mod_icons.jar_icon(self.file))).size, (16, 16))
        with Image.open(io.BytesIO(mod_icons.jar_icon(folder))) as image:
            self.assertEqual(image.size, (16, 16))
        self.assertEqual(list(folder.iterdir()), [icon])


class TestPackBrowserPayload(unittest.TestCase):
    def test_shared_wiki_merge_rejects_cache_and_protects_a_drastically_smaller_catalog(self):
        from tests.test_packs_browser import TEXT
        with tempfile.TemporaryDirectory() as folder:
            db = packs.PacksDB(Path(folder) / 'packs_db.json')
            db.packs = [fake_entry(f'old-{i}', name=f'Historical {i}') for i in range(10)]
            db.meta['wiki_fetched_at'] = 'KEEP'
            with self.assertRaises(packs.PacksError):
                packs.apply_wiki_result(db, SimpleNamespace(source='cache', text=TEXT))
            self.assertEqual(db.meta['wiki_fetched_at'], 'KEEP')
            _, warnings, _ = packs.apply_wiki_result(db, SimpleNamespace(source='online', text=TEXT))
            self.assertTrue(any('明显减少' in w for w in warnings))
            self.assertTrue(all(not db.get(f'old-{i}').get('wiki_removed') for i in range(10)))

    def test_cli_cache_result_never_merges_or_reports_online_completion(self):
        from gtnhmod import cli
        app = SimpleNamespace(cfg=object(), packs=mock.Mock(), ui=mock.Mock())
        with mock.patch.object(cli.packwiki, 'fetch_packs', return_value=SimpleNamespace(source='cache')):
            cli.CliApp._packs_refresh_wiki(app)
        app.packs.merge_wiki.assert_not_called()
        self.assertFalse(any('在线完成' in str(c) for c in app.ui.info.call_args_list))

    def test_badges_distinguish_installed_disabled_locked_and_builtin(self):
        entry = fake_entry()
        self.assertEqual(packs_browser.installation_badges(entry, {})[0]['text'], '未安装')
        rec = {'file_name': 'a.zip.disabled', 'enabled': False, 'locked': True, 'version': None}
        texts = [x['text'] for x in packs_browser.installation_badges(entry, rec)]
        self.assertIn('文件已禁用', texts)
        self.assertIn('已锁定更新', texts)
        self.assertIn('版本未知', texts)
        self.assertIn('游戏内启用', packs_browser.installation_badges({**entry, 'local': True}, {})[0]['text'])

    def test_payload_excludes_custom_removed_paths_and_private_metadata(self):
        item = fake_entry()
        item.update(secret='PRIVATE_TOKEN', path='PRIVATE_PATH')
        entries = [item, fake_entry('custom', custom=True), fake_entry('removed', wiki_removed=True)]
        payload = packs_browser.catalog_payload(entries, {('resourcepack', item['id']): {
            'file_name': 'PRIVATE_FILE', 'path': 'PRIVATE_PATH', 'version': '1.0'}})
        self.assertEqual(len(payload), 1)
        self.assertTrue(payload[0]['installed'])
        self.assertNotIn('PRIVATE', json.dumps(payload))


if __name__ == '__main__':
    unittest.main()
