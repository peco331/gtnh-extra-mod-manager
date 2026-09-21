# Concurrency and cache test
import io
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from gtnhmod.config import Config
from gtnhmod.db import ModsDB
from gtnhmod.installed import InstalledDB
from gtnhmod import updater
from gtnhmod import downloader
from gtnhmod.sources import DownloadCandidate, Source, UpdateInfo


def _make_dummy_jar(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('test.txt', 'dummy content')
    path.write_bytes(buf.getvalue())


class TestUpdaterConcurrencyAndCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='gtnh_conc_'))
        self.data = self.tmp / 'data'
        self.client_mods = self.tmp / 'client' / 'mods'
        self.server_mods = self.tmp / 'server' / 'mods'
        for d in (self.data, self.client_mods, self.server_mods):
            d.mkdir(parents=True)
        self.cfg = Config(self.data)
        self.cfg.set_mods_dir('client', self.client_mods)
        self.cfg.set_mods_dir('server', self.server_mods)
        self.db = ModsDB(self.data / 'mods_db.json')
        self.installed = InstalledDB(self.data / 'installed.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_updates_deduplicates_across_sides(self):
        _make_dummy_jar(self.client_mods / 'SharedMod-1.7.10-1.0.jar')
        _make_dummy_jar(self.server_mods / 'SharedMod-1.7.10-1.0.jar')
        mid = self.db.add_custom({
            'name_en': 'SharedMod', 'name_cn': 'SharedMod', 'side': 'both',
            'source_type': 'manual'
        })

        mock_check = MagicMock(return_value=UpdateInfo('1.1', [], '', 'now', 'ok'))
        with patch.object(Source, 'from_entry') as mock_from_entry:
            mock_source = MagicMock()
            mock_source.check = mock_check
            mock_from_entry.return_value = mock_source

            results = updater.check_updates(self.cfg, self.db, self.installed, force=True)

            self.assertEqual(len(results), 2)
            sides_reported = {r[0] for r in results}
            self.assertEqual(sides_reported, {'client', 'server'})
            self.assertEqual(mock_check.call_count, 1)

    def test_check_updates_concurrent_execution(self):
        for i in range(4):
            name = f'Mod{i}'
            _make_dummy_jar(self.client_mods / f'{name}-1.7.10-1.0.jar')
            self.db.add_custom({
                'name_en': name, 'side': 'client', 'source_type': 'manual'
            })

        def slow_check(*args, **kwargs):
            time.sleep(0.1)
            return UpdateInfo('1.1', [], '', 'now', 'ok')

        with patch.object(Source, 'from_entry') as mock_from_entry:
            mock_source = MagicMock()
            mock_source.check = slow_check
            mock_from_entry.return_value = mock_source

            t0 = time.perf_counter()
            results = updater.check_updates(self.cfg, self.db, self.installed, force=True)
            elapsed = time.perf_counter() - t0

            self.assertEqual(len(results), 4)
            self.assertLess(elapsed, 0.35)

    def test_download_cache_reuses_file_across_sides(self):
        cand_jar = self.tmp / 'RemoteMod-1.7.10-2.0.jar'
        _make_dummy_jar(cand_jar)
        cand = DownloadCandidate(cand_jar.as_uri(), 'RemoteMod-1.7.10-2.0.jar')

        backup_c = self.tmp / 'backup_client'
        backup_s = self.tmp / 'backup_server'
        dl_cache = self.tmp / 'dl_cache'

        with patch('gtnhmod.net.download', wraps=downloader.net.download) as mock_dl:
            dest1, failed1 = downloader.update_with_backup(
                cand, self.client_mods, backup_c, dl_cache_dir=dl_cache)
            self.assertTrue(dest1.exists())
            self.assertEqual(mock_dl.call_count, 1)

            dest2, failed2 = downloader.update_with_backup(
                cand, self.server_mods, backup_s, dl_cache_dir=dl_cache)
            self.assertTrue(dest2.exists())
            self.assertEqual(mock_dl.call_count, 1)


if __name__ == '__main__':
    unittest.main()
