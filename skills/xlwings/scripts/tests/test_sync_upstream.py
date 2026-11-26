# -*- coding: utf-8 -*-
"""sync_upstream 的离线安全回归测试；所有网络边界均被 mock。"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import sync_upstream as sync  # noqa: E402


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def entry(data: bytes) -> dict:
    return {"sha": blob_sha(data), "size": len(data)}


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9Z6Z8AAAAASUVORK5CYII=")


def tar_bytes(files: dict[str, bytes], root: str = "repo-sha") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class SyncUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gh_patch = mock.patch.object(sync, "GH", "")
        self.gh_patch.start()
        self.addCleanup(self.gh_patch.stop)

    def test_index_tree_preserves_blob_sha_and_size(self):
        data = {"truncated": False, "tree": [
            {"type": "blob", "path": "pkg/a.py", "sha": "a" * 40, "size": 7},
            {"type": "tree", "path": "pkg", "sha": "b" * 40},
        ]}
        with mock.patch.object(sync, "_gh_api", return_value=data):
            tree = sync.index_tree("owner/repo", "c" * 40)
        self.assertEqual(tree, {"pkg/a.py": {"sha": "a" * 40, "size": 7}})

    def test_index_tree_rejects_missing_or_malformed_blob_metadata(self):
        good = {"type": "blob", "path": "a.py", "sha": "a" * 40, "size": 1}
        cases = [
            None,
            {},
            {"truncated": False},
            {"truncated": False, "tree": [{k: v for k, v in good.items() if k != "sha"}]},
            {"truncated": False, "tree": [{k: v for k, v in good.items() if k != "size"}]},
            {"truncated": False, "tree": [{**good, "sha": "not-a-sha"}]},
            {"truncated": False, "tree": [{**good, "size": -1}]},
            {"truncated": False, "tree": [{**good, "size": True}]},
            {"truncated": False, "tree": [good, good]},
        ]
        for data in cases:
            with self.subTest(data=data), mock.patch.object(sync, "_gh_api", return_value=data):
                self.assertIsNone(sync.index_tree("owner/repo", "c" * 40))

    def test_index_tree_rejects_unsafe_paths(self):
        for path in ("../x", "/x", "pkg/../x", "pkg//x", "pkg\\x", "C:/x", "a:b", ""):
            data = {"truncated": False, "tree": [
                {"type": "blob", "path": path, "sha": "a" * 40, "size": 1},
            ]}
            with self.subTest(path=path), mock.patch.object(sync, "_gh_api", return_value=data):
                self.assertIsNone(sync.index_tree("owner/repo", "c" * 40))

    def test_tree_for_subpath_maps_only_directory_contents(self):
        a, b, c = entry(b"a"), entry(b"b"), entry(b"c")
        tree = {"pkg/a.py": a, "pkg/nested/b.py": b, "pkg2/c.py": c, "README.md": c}
        self.assertEqual(sync.tree_for_subpath(tree, "pkg/"), {"a.py": a, "nested/b.py": b})
        self.assertEqual(sync.tree_for_subpath(tree, ""), tree)

    def test_tree_for_subpath_rejects_invalid_or_missing_directory(self):
        tree = {"pkg/a.py": entry(b"a")}
        for subpath in (None, "../pkg", "/pkg", "pkg//", "pkg\\x", "C:/pkg", "/", "missing"):
            with self.subTest(subpath=subpath):
                self.assertIsNone(sync.tree_for_subpath(tree, subpath))

    def test_compare_changes_rejects_malformed_or_unsafe_entries(self):
        good = {"filename": "pkg/a.py", "status": "modified", "previous_filename": ""}
        cases = [
            {},
            {"files": [{"status": "modified"}]},
            {"files": [{"filename": "pkg/a.py"}]},
            {"files": [{**good, "status": "unprocessed"}]},
            {"files": [{**good, "filename": "../a.py"}]},
            {"files": [{"filename": "pkg/b.py", "status": "renamed"}]},
            {"files": [{"filename": "pkg/b.py", "status": "renamed", "previous_filename": "../a.py"}]},
        ]
        for data in cases:
            with self.subTest(data=data), mock.patch.object(sync, "_gh_api", return_value=data):
                self.assertIsNone(sync.compare_changes("owner/repo", "a" * 40, "b" * 40))

    def test_git_blob_sha_uses_git_object_header(self):
        self.assertEqual(sync.git_blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")

    def test_verify_local_tree_checks_blob_sha_not_just_count(self):
        target = self.root / "snapshot"
        (target / "pkg").mkdir(parents=True)
        (target / "pkg/a.py").write_bytes(b"good")
        (target / "pkg/b.py").write_bytes(b"stay")
        expected = {"pkg/a.py": entry(b"good"), "pkg/b.py": entry(b"stay")}
        self.assertIsNone(sync.verify_local_tree(target, expected))
        (target / "pkg/a.py").write_bytes(b"evil")  # 同数量、同字节数
        mismatch = sync.verify_local_tree(target, expected)
        self.assertIsNotNone(mismatch)
        self.assertIn("pkg/a.py", mismatch)

    def test_verify_local_tree_detects_missing_and_extra_files(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"a")
        (target / "c.py").write_bytes(b"c")
        expected = {"a.py": entry(b"a"), "b.py": entry(b"b")}
        mismatch = sync.verify_local_tree(target, expected)
        self.assertIsNotNone(mismatch)
        self.assertIn("b.py", mismatch)
        self.assertIn("c.py", mismatch)

    def test_verify_local_tree_honors_keep_whitelist(self):
        target = self.root / "snapshot"
        (target / "extracted").mkdir(parents=True)
        (target / "a.py").write_bytes(b"a")
        (target / "extracted/custom.txt").write_bytes(b"local")
        self.assertIsNone(sync.verify_local_tree(target, {"a.py": entry(b"a")}, ("extracted",)))

    def test_local_incomplete_uses_filtered_blob_baseline(self):
        target = self.root / "snapshot"
        target.mkdir(parents=True)
        (target / "a.py").write_bytes(b"good")
        src = {"type": "git", "local_dir": "snapshot", "repo": "owner/repo",
               "pinned_sha": "a" * 40, "subpath": "pkg"}
        data = {"truncated": False, "tree": [
            {"type": "blob", "path": "pkg/a.py", **entry(b"good")},
            {"type": "blob", "path": "outside.py", **entry(b"outside")},
        ]}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "_gh_api", return_value=data):
            self.assertFalse(sync.local_incomplete(src))
            (target / "a.py").write_bytes(b"evil")
            self.assertTrue(sync.local_incomplete(src))

    def test_incremental_rejects_same_size_local_tampering_before_download(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"evil")
        (target / "stay.py").write_bytes(b"stay")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": ""}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"a.py": entry(b"good"), "stay.py": entry(b"stay")}
        new_tree = {"a.py": entry(b"new!"), "stay.py": entry(b"stay")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "a.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw") as fetch:
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertFalse(ok)
        self.assertIn("blob", desc)
        self.assertEqual((target / "a.py").read_bytes(), b"evil")
        fetch.assert_not_called()

    def test_incremental_filters_old_and_new_trees_by_subpath(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"old!")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": "pkg"}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"pkg/a.py": entry(b"old!"), "outside.py": entry(b"same")}
        new_tree = {"pkg/a.py": entry(b"new!"), "outside.py": entry(b"diff")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "pkg/a.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw", return_value=b"new!") as fetch:
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertTrue(ok, desc)
        self.assertEqual((target / "a.py").read_bytes(), b"new!")
        fetch.assert_called_once_with("owner/repo", new_sha, "pkg/a.py")

    def test_incremental_rename_out_of_subpath_deletes_old_local_file(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"move")
        (target / "stay.py").write_bytes(b"stay")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": "pkg"}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"pkg/a.py": entry(b"move"), "pkg/stay.py": entry(b"stay")}
        new_tree = {"outside/a.py": entry(b"move"), "pkg/stay.py": entry(b"stay")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "outside/a.py", "status": "renamed", "previous_path": "pkg/a.py"}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw") as fetch:
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertTrue(ok, desc)
        self.assertFalse((target / "a.py").exists())
        self.assertEqual((target / "stay.py").read_bytes(), b"stay")
        fetch.assert_not_called()

    def test_incremental_rejects_same_size_raw_content_with_wrong_blob_sha(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"old!")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": ""}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"a.py": entry(b"old!")}
        new_tree = {"a.py": entry(b"new!")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "a.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw", return_value=b"bad!"):
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertFalse(ok)
        self.assertIn("blob", desc)
        self.assertEqual((target / "a.py").read_bytes(), b"old!")

    def test_incremental_stage_write_failure_preserves_entire_old_target(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"old!")
        (target / "stay.py").write_bytes(b"stay")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": ""}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"a.py": entry(b"old!"), "stay.py": entry(b"stay")}
        new_tree = {"a.py": entry(b"new!"), "stay.py": entry(b"stay")}
        original_write = Path.write_bytes

        def fail_write(path, data):
            if path.name == "a.py":
                raise OSError("simulated disk full")
            return original_write(path, data)

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "a.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw", return_value=b"new!"), \
                mock.patch.object(Path, "write_bytes", fail_write):
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertFalse(ok)
        self.assertIn("stage", desc)
        self.assertEqual((target / "a.py").read_bytes(), b"old!")
        self.assertEqual((target / "stay.py").read_bytes(), b"stay")

    def test_replace_dir_rename_failure_never_deletes_original_target(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "sentinel.txt").write_bytes(b"original")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        original_rename = Path.rename

        def fail_target_rename(path, destination):
            if path == target:
                raise PermissionError("simulated locked directory")
            return original_rename(path, destination)

        with mock.patch.object(Path, "rename", fail_target_rename), \
                mock.patch.object(sync.shutil, "rmtree", wraps=sync.shutil.rmtree) as remove:
            ok = sync.replace_dir(stage, target)
        self.assertFalse(ok)
        self.assertEqual((target / "sentinel.txt").read_bytes(), b"original")
        self.assertFalse(any(call.args and Path(call.args[0]) == target for call in remove.call_args_list))

    def test_replace_dir_keep_restore_failure_rolls_back(self):
        target = self.root / "snapshot"
        (target / "extracted").mkdir(parents=True)
        (target / "old.txt").write_bytes(b"old")
        (target / "extracted/custom.txt").write_bytes(b"keep")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        with mock.patch.object(sync, "_restore_keep", side_effect=OSError("simulated copy failure")):
            ok = sync.replace_dir(stage, target, ("extracted",))
        self.assertFalse(ok)
        self.assertEqual((target / "old.txt").read_bytes(), b"old")
        self.assertEqual((target / "extracted/custom.txt").read_bytes(), b"keep")
        self.assertFalse((target / "new.txt").exists())
        self.assertFalse(target.with_name(target.name + ".old").exists())

    def test_incremental_outside_only_change_still_checks_local_baseline(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"evil")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": "pkg"}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"pkg/a.py": entry(b"good"), "outside.py": entry(b"old")}
        new_tree = {"pkg/a.py": entry(b"good"), "outside.py": entry(b"new")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "outside.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw") as fetch:
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertFalse(ok)
        self.assertIn("blob", desc)
        fetch.assert_not_called()

    def test_download_tarball_addresses_detected_sha_on_every_channel(self):
        sha = "c" * 40
        archive = tar_bytes({"a.txt": b"a"})
        urls = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "curl.exe":
                urls.append(cmd[4])
                Path(cmd[3]).write_bytes(archive)
                return subprocess.CompletedProcess(cmd, 0, "200", "")
            if cmd[0] == "tar":
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
                    tf.extractall(cmd[-1], filter="data")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(cmd)

        with mock.patch.object(sync, "run", side_effect=fake_run):
            stage, channel = sync.download_tarball("owner/repo", sha)
        self.assertIsNotNone(stage)
        if stage is not None:
            self.addCleanup(sync.shutil.rmtree, stage.parent, ignore_errors=True)
        self.assertEqual(channel, "codeload")
        self.assertTrue(urls)
        for url in urls:
            self.assertIn(f"/{sha}", url)
            self.assertNotIn("refs/", url)

    def test_download_tarball_rejects_truncated_gzip_before_trusting_extractor(self):
        sha = "c" * 40
        valid = tar_bytes({"a.txt": b"a"})
        downloads = []
        extracted = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "curl.exe":
                downloads.append(cmd[4])
                data = valid[:-8] if len(downloads) == 1 else valid
                Path(cmd[3]).write_bytes(data)
                return subprocess.CompletedProcess(cmd, 0, "200", "")
            if cmd[0] == "tar":
                extracted.append(Path(cmd[2]))
                # 故意模拟不可信 tar：即使 gzip 截断也报成功并创建根目录。
                (Path(cmd[-1]) / "repo-sha").mkdir(exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(cmd)

        with mock.patch.object(sync, "run", side_effect=fake_run):
            stage, channel = sync.download_tarball("owner/repo", sha)
        self.assertEqual(channel, "archive")
        self.assertIsNotNone(stage)
        if stage is not None:
            self.addCleanup(sync.shutil.rmtree, stage.parent, ignore_errors=True)
        self.assertEqual(len(downloads), 2)
        self.assertEqual(extracted, [])  # gzip 失败包不得交给任何解包器

    def test_download_tarball_rejects_unpacked_file_count_mismatch(self):
        sha = "c" * 40
        archive = tar_bytes({"a.txt": b"a"})
        original_mkdtemp = sync.tempfile.mkdtemp

        def fake_run(cmd, **kwargs):
            if cmd[0] == "curl.exe":
                Path(cmd[3]).write_bytes(archive)
                return subprocess.CompletedProcess(cmd, 0, "200", "")
            if cmd[0] == "tar":
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
                    tf.extractall(cmd[-1], filter="data")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(cmd)

        with mock.patch.object(sync, "run", side_effect=fake_run), \
                mock.patch.object(sync.tempfile, "mkdtemp", side_effect=lambda **kw: original_mkdtemp(dir=self.root, **kw)):
            stage, channel = sync.download_tarball("owner/repo", sha, expected_files=2)
        self.assertIsNone(stage)
        self.assertEqual(channel, "")

    def test_download_tarball_does_not_rely_on_system_tar(self):
        sha = "c" * 40
        archive = tar_bytes({"a.txt": b"a"})

        def fake_run(cmd, **kwargs):
            if cmd[0] == "curl.exe":
                Path(cmd[3]).write_bytes(archive)
                return subprocess.CompletedProcess(cmd, 0, "200", "")
            raise AssertionError(f"unexpected external extractor: {cmd}")

        with mock.patch.object(sync, "run", side_effect=fake_run):
            stage, channel = sync.download_tarball("owner/repo", sha, expected_files=1)
        self.assertEqual(channel, "codeload")
        self.assertIsNotNone(stage)
        if stage is not None:
            self.addCleanup(sync.shutil.rmtree, stage.parent, ignore_errors=True)
            self.assertEqual((stage / "a.txt").read_bytes(), b"a")

    def test_download_tarball_without_verified_tree_never_uses_proxy(self):
        urls = []

        def unavailable(cmd, **kwargs):
            self.assertEqual(cmd[0], "curl.exe")
            urls.append(cmd[4])
            return subprocess.CompletedProcess(cmd, 0, "503", "")

        with mock.patch.object(sync, "run", side_effect=unavailable):
            stage, channel = sync.download_tarball("owner/repo", "c" * 40)
        self.assertIsNone(stage)
        self.assertEqual(channel, "")
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(url.startswith("https://codeload.github.com/")
                            or url.startswith("https://github.com/") for url in urls))
        self.assertFalse(any("ghfast.top" in url for url in urls))

    def test_download_tarball_verified_tree_allows_proxy_after_official_failures(self):
        sha = "c" * 40
        archive = tar_bytes({"a.txt": b"a"})
        urls = []

        def fake_run(cmd, **kwargs):
            self.assertEqual(cmd[0], "curl.exe")
            urls.append(cmd[4])
            if "ghfast.top" in cmd[4]:
                Path(cmd[3]).write_bytes(archive)
                return subprocess.CompletedProcess(cmd, 0, "200", "")
            return subprocess.CompletedProcess(cmd, 0, "503", "")

        with mock.patch.object(sync, "run", side_effect=fake_run):
            stage, channel = sync.download_tarball("owner/repo", sha, expected_files=1,
                                                   tree_verified=True)
        self.assertEqual(channel, "ghfast")
        self.assertIsNotNone(stage)
        if stage is not None:
            self.addCleanup(sync.shutil.rmtree, stage.parent, ignore_errors=True)
        self.assertEqual(len(urls), 3)
        self.assertIn("ghfast.top", urls[-1])

    def test_sync_one_full_fallback_downloads_detected_head_sha(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "", "pinned_sha": "a" * 40}
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "a.txt").write_bytes(b"new")
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "index_tree", return_value=None), \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")) as download, \
                mock.patch.object(sync, "replace_dir", return_value=True):
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "updated")
        download.assert_called_once_with("owner/repo", head, expected_files=None, tree_verified=False)
        self.assertEqual(src["pinned_sha"], head)

    def test_sync_one_full_fallback_passes_expected_tree_file_count(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "", "pinned_sha": "a" * 40}
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "a.txt").write_bytes(b"new")
        tree = {"a.txt": entry(b"new")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "index_tree", return_value=tree) as index, \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")) as download, \
                mock.patch.object(sync, "replace_dir", return_value=True):
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "updated")
        index.assert_called_once_with("owner/repo", head)
        download.assert_called_once_with("owner/repo", head, expected_files=1, tree_verified=True)

    def test_sync_one_full_fallback_selects_subpath_stage(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "pkg", "pinned_sha": "a" * 40}
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        repo_stage = self.root / "repo-root"
        (repo_stage / "pkg").mkdir(parents=True)
        (repo_stage / "pkg/a.txt").write_bytes(b"new")
        (repo_stage / "outside.txt").write_bytes(b"outside")
        tree = {"pkg/a.txt": entry(b"new"), "outside.txt": entry(b"outside")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "index_tree", return_value=tree), \
                mock.patch.object(sync, "download_tarball", return_value=(repo_stage, "codeload")):
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "updated")
        self.assertEqual((target / "a.txt").read_bytes(), b"new")
        self.assertFalse((target / "outside.txt").exists())
        self.assertFalse((target / "pkg").exists())

    def test_sync_one_full_stage_verifies_blob_sha_before_replace(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "", "pinned_sha": "a" * 40}
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.txt").write_bytes(b"old!")
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "a.txt").write_bytes(b"bad!")
        tree = {"a.txt": entry(b"new!")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "index_tree", return_value=tree), \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")), \
                mock.patch.object(sync, "replace_dir", return_value=True) as replace:
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "failed")
        replace.assert_not_called()
        self.assertEqual((target / "a.txt").read_bytes(), b"old!")
        self.assertEqual(src["pinned_sha"], "a" * 40)

    def test_latest_sha_rejects_malformed_gh_output_and_uses_git_fallback(self):
        valid = "b" * 40
        responses = [
            subprocess.CompletedProcess([], 0, "not-a-sha\n", ""),
            subprocess.CompletedProcess([], 0, f"{valid}\trefs/tags/main^{{}}\n", ""),
        ]
        with mock.patch.object(sync, "GH", "gh"), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", side_effect=responses) as run:
            self.assertEqual(sync.latest_sha("owner/repo", "main"), valid)
        self.assertEqual(run.call_count, 2)

    def test_incremental_download_exception_returns_failure_without_touching_target(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"old!")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": ""}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"a.py": entry(b"old!")}
        new_tree = {"a.py": entry(b"new!")}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "a.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw", side_effect=RuntimeError("simulated network error")):
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertFalse(ok)
        self.assertIn("下载异常", desc)
        self.assertEqual((target / "a.py").read_bytes(), b"old!")

    def test_replace_dir_partial_copy_failure_rolls_back(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        original_copy = sync.shutil.copytree

        def partial_copy(src, dst, *args, **kwargs):
            original_copy(src, dst, *args, **kwargs)
            (Path(dst) / "partial.txt").write_bytes(b"partial")
            raise OSError("simulated copy failure")

        with mock.patch.object(sync.shutil, "copytree", side_effect=partial_copy):
            ok = sync.replace_dir(stage, target)
        self.assertFalse(ok)
        self.assertEqual((target / "old.txt").read_bytes(), b"old")
        self.assertFalse((target / "new.txt").exists())
        self.assertFalse((target / "partial.txt").exists())
        self.assertFalse(target.with_name(target.name + ".old").exists())

    def test_replace_dir_count_mismatch_rolls_back(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        with mock.patch.object(sync, "count_files", side_effect=[2, 1]):
            ok = sync.replace_dir(stage, target)
        self.assertFalse(ok)
        self.assertEqual((target / "old.txt").read_bytes(), b"old")
        self.assertFalse((target / "new.txt").exists())
        self.assertFalse(target.with_name(target.name + ".old").exists())

    def test_incremental_verifies_entire_stage_not_only_changed_files(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"old!")
        (target / "stay.py").write_bytes(b"stay")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": ""}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"a.py": entry(b"old!"), "stay.py": entry(b"stay")}
        new_tree = {"a.py": entry(b"new!"), "stay.py": entry(b"stay")}
        original_copy = sync.shutil.copytree

        def corrupt_copy(src, dst, *args, **kwargs):
            original_copy(src, dst, *args, **kwargs)
            (Path(dst) / "stay.py").write_bytes(b"evil")

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "a.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw", return_value=b"new!"), \
                mock.patch.object(sync.shutil, "copytree", side_effect=corrupt_copy):
            ok, desc, _ = sync.sync_incremental(src, old_sha, new_sha)
        self.assertFalse(ok)
        self.assertIn("stage", desc)
        self.assertEqual((target / "a.py").read_bytes(), b"old!")
        self.assertEqual((target / "stay.py").read_bytes(), b"stay")

    def test_safe_extractor_rejects_traversal_links_conflicts_and_multiple_roots(self):
        cases = [
            [("file", "../escape.txt", b"x", "")],
            [("file", "/absolute.txt", b"x", "")],
            [("file", "repo\\escape.txt", b"x", "")],
            [("symlink", "repo-sha/link", b"", "a.txt")],
            [("file", "repo-sha/a.txt", b"a", ""), ("file", "other/b.txt", b"b", "")],
            [("file", "repo-sha/a.txt", b"a", ""), ("file", "repo-sha/a.txt", b"b", "")],
            [("file", "repo-sha/a", b"a", ""), ("file", "repo-sha/a/b.txt", b"b", "")],
        ]
        for index, members in enumerate(cases):
            with self.subTest(index=index):
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz") as archive:
                    for kind, name, data, link in members:
                        info = tarfile.TarInfo(name)
                        info.mode = 0o644
                        if kind == "symlink":
                            info.type = tarfile.SYMTYPE
                            info.linkname = link
                            archive.addfile(info)
                        else:
                            info.size = len(data)
                            archive.addfile(info, io.BytesIO(data))
                tarball = self.root / f"bad-{index}.tar.gz"
                tarball.write_bytes(buf.getvalue())
                dest = self.root / f"out-{index}"
                dest.mkdir()
                self.assertIsNone(sync._extract_tarball(tarball, dest, None))
                self.assertFalse((self.root / "escape.txt").exists())

    def _web_src(self):
        return {
            "id": "web-demo", "type": "web", "local_dir": "web",
            "base_url": "https://docs.example/", "llms_url": "https://docs.example/llms-full.txt",
            "index_url": "https://docs.example/llms.txt",
            "pinned_version": "1.0.0.1", "pinned_full_sha": "0" * 64,
            "pinned_pages": ["old.md"],
        }

    def _web_full(self, names=("index.md", "changelog.md", "guide.md"), version="1.2.3.4"):
        parts = []
        for name in names:
            title = name[:-3].replace("-", " ").title()
            body = f"## {version} (2026-11-18)\n\nLatest changes.\n" if name == "changelog.md" else f"Body for {title}.\n"
            parts.append(f"# {name}\n\n# {title}\n\n{body}")
        return "\n".join(parts)

    def _web_index(self, names=("index.md", "changelog.md", "guide.md")):
        return "\n".join(f"- [{name[:-3].title()}]({name}): summary" for name in names)

    def _mock_web_io(self, src, full, index):
        docs = {src["llms_url"]: full, src["index_url"]: index}
        return mock.patch.object(sync, "_curl_text", side_effect=lambda url, timeout=120: docs.get(url))

    def test_web_latest_accepts_complete_documents(self):
        src = self._web_src()
        full = self._web_full()
        with self._mock_web_io(src, full, self._web_index()):
            info = sync.web_latest(src)
        self.assertIsNotNone(info)
        self.assertEqual(info["top_version"], "1.2.3.4")
        self.assertEqual(info["page_names"], {"index.md", "changelog.md", "guide.md"})
        self.assertEqual(info["full_sha"], hashlib.sha256(full.encode("utf-8")).hexdigest())

    def test_web_latest_rejects_response_without_page_markers(self):
        src = self._web_src()
        with self._mock_web_io(src, "<html>upstream error</html>", self._web_index()):
            self.assertIsNone(sync.web_latest(src))

    def test_web_latest_rejects_missing_changelog_or_version(self):
        src = self._web_src()
        cases = [
            (self._web_full(("index.md", "guide.md")), self._web_index(("index.md", "guide.md"))),
            (self._web_full().replace("## 1.2.3.4 (2026-11-18)", "No release heading"), self._web_index()),
        ]
        for full, index in cases:
            with self.subTest(full=full), self._mock_web_io(src, full, index):
                self.assertIsNone(sync.web_latest(src))

    def test_web_latest_rejects_whitespace_only_page_before_building_mirror(self):
        src = self._web_src()
        full = self._web_full().replace("# guide.md\n\n# Guide\n\nBody for Guide.\n",
                                        "# guide.md\n\n \n\t\n")
        with self._mock_web_io(src, full, self._web_index()), \
                mock.patch.object(sync.web_mirror, "build_mirror", wraps=sync.web_mirror.build_mirror) as build:
            info = sync.web_latest(src)
        self.assertIsNone(info)
        build.assert_not_called()

    def test_web_latest_rejects_index_page_set_mismatch(self):
        src = self._web_src()
        full = self._web_full()
        indexes = [
            self._web_index(("index.md", "changelog.md")),
            self._web_index(("index.md", "changelog.md", "guide.md", "extra.md")),
        ]
        for index in indexes:
            with self.subTest(index=index), self._mock_web_io(src, full, index):
                self.assertIsNone(sync.web_latest(src))

    def test_web_latest_rejects_unavailable_or_malformed_index(self):
        src = self._web_src()
        full = self._web_full()
        duplicate = self._web_index() + "\n- [Guide again](guide.md): duplicate"
        for index in (None, "", "<html>index error</html>", duplicate):
            with self.subTest(index=index), self._mock_web_io(src, full, index):
                self.assertIsNone(sync.web_latest(src))

    def test_web_latest_rejects_duplicate_page_markers(self):
        src = self._web_src()
        full = self._web_full() + "\n# guide.md\n\n# Duplicate\n"
        with self._mock_web_io(src, full, self._web_index()):
            self.assertIsNone(sync.web_latest(src))

    def test_web_rejects_old_image_symlink_before_copy(self):
        src = self._web_src()
        target = self.root / "web"
        image = target / "_images/nested/pic.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(PNG_1X1)
        (target / "old.md").write_bytes(b"old mirror")
        info = {"pages": {"new.md": "# New\n"}, "top_version": "1.2.3.4",
                "page_names": {"new.md"}, "images": {"pic.png"}, "full_sha": "f" * 64}
        real_is_symlink = Path.is_symlink

        def simulated_symlink(path):
            return True if path == image else real_is_symlink(path)

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "web_latest", return_value=info), \
                mock.patch.object(Path, "is_symlink", simulated_symlink), \
                mock.patch.object(sync.shutil, "copytree", wraps=sync.shutil.copytree) as copy, \
                mock.patch.object(sync, "_curl_bytes", return_value=False) as download, \
                mock.patch.object(sync, "replace_dir", return_value=False):
            result = sync.sync_web_one(src, force=True)
        self.assertEqual(result, "failed")
        copy.assert_not_called()
        download.assert_not_called()
        self.assertEqual(image.read_bytes(), PNG_1X1)
        self.assertEqual((target / "old.md").read_bytes(), b"old mirror")

    def test_curl_bytes_rejects_symlink_before_download(self):
        out = self.root / "stage/_images/pic.png"
        out.parent.mkdir(parents=True)
        real_is_symlink = Path.is_symlink

        def simulated_symlink(path):
            return True if path == out else real_is_symlink(path)

        with mock.patch.object(Path, "is_symlink", simulated_symlink), \
                mock.patch.object(sync, "_http_get", return_value=PNG_1X1) as download:
            result = sync._curl_bytes("https://docs.example/_images/pic.png", out)
        self.assertFalse(result)
        download.assert_not_called()
        self.assertFalse(out.exists())

    def test_curl_bytes_rechecks_symlink_after_download_and_preserves_external_sentinel(self):
        out = self.root / "stage/_images/pic.png"
        out.parent.mkdir(parents=True)
        sentinel = self.root / "outside.png"
        sentinel.write_bytes(b"external sentinel")
        state = {"symlink": False}
        real_is_symlink, real_open = Path.is_symlink, Path.open
        followed_writes = []

        def simulated_symlink(path):
            return state["symlink"] if path == out else real_is_symlink(path)

        def download(url, timeout, **kwargs):
            state["symlink"] = True
            return PNG_1X1

        def followed_open(path, mode="r", *args, **kwargs):
            if path == out and any(flag in mode for flag in "wax+"):
                followed_writes.append(path)
                return sentinel.open("wb")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(sync, "_http_get", side_effect=download), \
                mock.patch.object(Path, "is_symlink", simulated_symlink), \
                mock.patch.object(Path, "open", followed_open):
            result = sync._curl_bytes("https://docs.example/_images/pic.png", out)
        self.assertFalse(result)
        self.assertEqual(followed_writes, [])
        self.assertEqual(sentinel.read_bytes(), b"external sentinel")
        self.assertFalse(out.exists())

    def test_http_get_rejects_declared_length_over_limit(self):
        response = mock.MagicMock()
        response.headers = {"Content-Length": "4"}
        response.geturl.return_value = "https://docs.example/llms.txt"
        response.read.return_value = b"abcd"
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "MAX_HTTP_DOWNLOAD_BYTES", 3, create=True), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            self.assertIsNone(sync._http_get("https://docs.example/llms.txt", 1))
        response.read.assert_not_called()

    def test_http_get_rejects_chunked_stream_over_limit(self):
        response = io.BytesIO(b"abcdef")
        response.headers = {}
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "MAX_HTTP_DOWNLOAD_BYTES", 3, create=True), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            self.assertIsNone(sync._http_get("https://docs.example/llms.txt", 1))

    def test_http_get_rejects_content_length_mismatch(self):
        response = io.BytesIO(b"abc")
        response.headers = {"Content-Length": "4"}
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            self.assertIsNone(sync._http_get("https://docs.example/llms.txt", 1))

    def test_http_get_rejects_https_downgrade(self):
        response = io.BytesIO(b"abc")
        response.headers = {}
        response.geturl = lambda: "http://docs.example/llms.txt"
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            self.assertIsNone(sync._http_get("https://docs.example/llms.txt", 1))

    def test_fetch_raw_rejects_stream_over_limit(self):
        response = io.BytesIO(b"abcdef")
        response.headers = {}
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "MAX_HTTP_DOWNLOAD_BYTES", 3, create=True), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            self.assertIsNone(sync.fetch_raw("owner/repo", "a" * 40, "a.txt"))

    def test_fetch_raw_accepts_empty_blob(self):
        response = io.BytesIO(b"")
        response.headers = {}
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            self.assertEqual(sync.fetch_raw("owner/repo", "a" * 40, "empty.txt"), b"")

    def test_curl_bytes_rejects_non_image_content_type(self):
        out = self.root / "stage/pic.png"
        out.parent.mkdir()
        response = io.BytesIO(PNG_1X1)
        response.headers = mock.Mock()
        response.headers.get_content_type.return_value = "text/html"
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            result = sync._curl_bytes("https://docs.example/_images/pic.png", out)
        self.assertFalse(result)
        self.assertFalse(out.exists())

    def test_curl_bytes_rejects_html_body_with_image_content_type(self):
        out = self.root / "stage/pic.png"
        out.parent.mkdir()
        response = io.BytesIO(b"<html><body>upstream error</body></html>")
        response.headers = mock.Mock()
        response.headers.get_content_type.return_value = "image/png"
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            result = sync._curl_bytes("https://docs.example/_images/pic.png", out)
        self.assertFalse(result)
        self.assertFalse(out.exists())

    def test_curl_bytes_rejects_content_type_and_signature_mismatch(self):
        out = self.root / "stage/pic.png"
        out.parent.mkdir()
        response = io.BytesIO(b"\xff\xd8\xff\xe0jpeg body")
        response.headers = mock.Mock()
        response.headers.get_content_type.return_value = "image/png"
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            result = sync._curl_bytes("https://docs.example/_images/pic.png", out)
        self.assertFalse(result)
        self.assertFalse(out.exists())

    def test_curl_bytes_accepts_svg_with_matching_content_type_and_root(self):
        out = self.root / "stage/logo.svg"
        out.parent.mkdir()
        data = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        response = io.BytesIO(data)
        response.headers = mock.Mock()
        response.headers.get_content_type.return_value = "image/svg+xml; charset=utf-8"
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "RETRY_SLEEP", 0):
            result = sync._curl_bytes("https://docs.example/_images/logo.svg", out)
        self.assertTrue(result)
        self.assertEqual(out.read_bytes(), data)

    def test_verify_web_stage_rejects_nonempty_html_image(self):
        stage = self.root / "web-stage"
        (stage / "_images").mkdir(parents=True)
        (stage / "new.md").write_bytes(b"# New\n")
        (stage / "_images/error.png").write_bytes(b"<html>error</html>")
        info = {"pages": {"new.md": "# New\n"}, "page_names": {"new.md"},
                "images": {"error.png"}}
        mismatch = sync._verify_web_stage(stage, info, None)
        self.assertIsNotNone(mismatch)
        self.assertIn("error.png", mismatch)

    def test_web_redownloads_and_validates_same_name_image(self):
        src = self._web_src()
        target = self.root / "web"
        (target / "_images").mkdir(parents=True)
        old_svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="red"/></svg>'
        new_svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="blue"/></svg>'
        (target / "_images/pic.svg").write_bytes(old_svg)
        (target / "old.md").write_bytes(b"old mirror")
        full = self._web_full().replace("Body for Guide.", "![Guide](images/pic.svg)")
        downloads = []

        def fake_image(url, out, timeout=120, **kwargs):
            downloads.append((url, out))
            out.write_bytes(new_svg)
            return True

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                self._mock_web_io(src, full, self._web_index()), \
                mock.patch.object(sync, "_curl_bytes", side_effect=fake_image):
            result = sync.sync_web_one(src, force=True)
        self.assertEqual(result, "updated")
        self.assertEqual(len(downloads), 1)
        self.assertEqual((target / "_images/pic.svg").read_bytes(), new_svg)

    def test_web_check_reports_local_damage_when_remote_pin_is_unchanged(self):
        src = self._web_src()
        with self._mock_web_io(src, self._web_full(), self._web_index()):
            info = sync.web_latest(src)
        src["pinned_version"] = info["top_version"]
        src["pinned_full_sha"] = info["full_sha"]
        src["pinned_local_sha256"] = "0" * 64
        target = self.root / "web"
        target.mkdir()
        for name, content in info["pages"].items():
            (target / name).write_text(content, encoding="utf-8")
        (target / "guide.md").write_text("damaged", encoding="utf-8")
        src["pinned_local_sha256"] = sync._web_fingerprint(target)
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "web_latest", return_value=info):
            state, result = sync._check_source(src)
        self.assertEqual(result, "updated")
        self.assertIn("本地", state)

    def test_web_check_detects_extra_local_file_from_fingerprint(self):
        src = self._web_src()
        with self._mock_web_io(src, self._web_full(), self._web_index()):
            info = sync.web_latest(src)
        src["pinned_version"] = info["top_version"]
        src["pinned_full_sha"] = info["full_sha"]
        src["pinned_local_sha256"] = "0" * 64
        target = self.root / "web"
        target.mkdir()
        for name, content in info["pages"].items():
            (target / name).write_bytes(content.encode("utf-8"))
        (target / "extra.bin").write_bytes(b"unexpected")
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "web_latest", return_value=info):
            state, result = sync._check_source(src)
        self.assertEqual(result, "updated")
        self.assertIn("本地", state)

    def test_web_sync_repairs_local_damage_when_remote_pin_is_unchanged(self):
        src = self._web_src()
        with self._mock_web_io(src, self._web_full(), self._web_index()):
            info = sync.web_latest(src)
        src["pinned_version"] = info["top_version"]
        src["pinned_full_sha"] = info["full_sha"]
        src["pinned_local_sha256"] = "0" * 64
        target = self.root / "web"
        target.mkdir()
        (target / "guide.md").write_text("damaged", encoding="utf-8")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"sources": [src]}), encoding="utf-8")
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "web_latest", return_value=info), \
                mock.patch.object(sync, "_curl_bytes") as download:
            result = sync.sync_web_one(src)
        self.assertEqual(result, "updated")
        self.assertEqual((target / "guide.md").read_text(encoding="utf-8"), info["pages"]["guide.md"])
        self.assertRegex(src["pinned_local_sha256"], r"^[0-9a-f]{64}$")
        download.assert_not_called()

    def test_web_sync_backfills_local_fingerprint_without_download(self):
        src = self._web_src()
        with self._mock_web_io(src, self._web_full(), self._web_index()):
            info = sync.web_latest(src)
        src["pinned_version"] = info["top_version"]
        src["pinned_full_sha"] = info["full_sha"]
        target = self.root / "web"
        target.mkdir()
        for name, content in info["pages"].items():
            (target / name).write_text(content, encoding="utf-8", newline="\n")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"sources": [src]}), encoding="utf-8")
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "web_latest", return_value=info), \
                mock.patch.object(sync, "_curl_bytes") as download:
            result = sync.sync_web_one(src)
        self.assertEqual(result, "unchanged")
        self.assertRegex(src["pinned_local_sha256"], r"^[0-9a-f]{64}$")
        download.assert_not_called()

    def test_web_image_failure_preserves_old_mirror_and_pins(self):
        src = self._web_src()
        target = self.root / "web"
        target.mkdir()
        (target / "old.md").write_bytes(b"old mirror")
        (target / "README.md").write_bytes(b"local index")
        manifest = self.root / "manifest.json"
        manifest.write_bytes(b"original manifest")
        info = {"pages": {"new.md": "# New\n"}, "top_version": "1.2.3.4",
                "page_names": {"new.md"}, "images": {"missing.png"},
                "full_sha": "f" * 64}
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "web_latest", return_value=info), \
                mock.patch.object(sync, "_curl_bytes", return_value=False):
            result = sync.sync_web_one(src, force=True)
        self.assertEqual(result, "failed")
        self.assertEqual((target / "old.md").read_bytes(), b"old mirror")
        self.assertFalse((target / "new.md").exists())
        self.assertEqual(src["pinned_version"], "1.0.0.1")
        self.assertEqual(src["pinned_full_sha"], "0" * 64)
        self.assertEqual(manifest.read_bytes(), b"original manifest")
        self.assertFalse((self.root / "SYNCLOG.md").exists())

    def test_web_builds_complete_sibling_stage_before_replace(self):
        src = self._web_src()
        target = self.root / "web"
        (target / "_images").mkdir(parents=True)
        (target / "old.md").write_bytes(b"old mirror")
        (target / "README.md").write_bytes(b"local index")
        (target / "_images/existing.png").write_bytes(PNG_1X1)
        full = self._web_full().replace("Body for Guide.", "![Guide](images/new.png)")
        captured = {}
        original_replace = sync.replace_dir

        def capture_stage(stage, destination, keep=()):
            captured["stage"] = stage
            captured["files"] = {p.relative_to(stage).as_posix(): p.read_bytes()
                                 for p in stage.rglob("*") if p.is_file()}
            return original_replace(stage, destination, keep)

        def fake_image(url, out, timeout=120):
            captured["image_out"] = out
            out.write_bytes(PNG_1X1)
            return True

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                self._mock_web_io(src, full, self._web_index()), \
                mock.patch.object(sync, "_curl_bytes", side_effect=fake_image), \
                mock.patch.object(sync, "replace_dir", side_effect=capture_stage):
            result = sync.sync_web_one(src, force=True)
        self.assertEqual(result, "updated")
        self.assertEqual(captured["stage"].parent.parent, target.parent)
        self.assertNotEqual(captured["image_out"].parent, target / "_images")
        self.assertEqual(captured["files"]["README.md"], b"local index")
        self.assertEqual(captured["files"]["_images/existing.png"], PNG_1X1)
        self.assertEqual(captured["files"]["_images/new.png"], PNG_1X1)
        self.assertIn("guide.md", captured["files"])
        self.assertNotIn("old.md", captured["files"])
        self.assertFalse((target / "old.md").exists())
        self.assertEqual((target / "_images/new.png").read_bytes(), PNG_1X1)

    def test_web_corrupt_stage_page_preserves_old_mirror(self):
        src = self._web_src()
        target = self.root / "web"
        target.mkdir()
        (target / "old.md").write_bytes(b"old mirror")
        info = {"pages": {"new.md": "# New\n"}, "top_version": "1.2.3.4",
                "page_names": {"new.md"}, "images": set(), "full_sha": "f" * 64}
        original_write = Path.write_text

        def corrupt_page(path, data, *args, **kwargs):
            if path.name == "new.md":
                data += "corrupt"
            return original_write(path, data, *args, **kwargs)

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "web_latest", return_value=info), \
                mock.patch.object(Path, "write_text", corrupt_page), \
                mock.patch.object(sync, "replace_dir", wraps=sync.replace_dir) as replace:
            result = sync.sync_web_one(src, force=True)
        self.assertEqual(result, "failed")
        replace.assert_not_called()
        self.assertEqual((target / "old.md").read_bytes(), b"old mirror")
        self.assertFalse((target / "new.md").exists())
        self.assertEqual(src["pinned_full_sha"], "0" * 64)

    def test_web_empty_downloaded_image_fails_stage_validation(self):
        src = self._web_src()
        target = self.root / "web"
        target.mkdir()
        (target / "old.md").write_bytes(b"old mirror")
        info = {"pages": {"new.md": "# New\n"}, "top_version": "1.2.3.4",
                "page_names": {"new.md"}, "images": {"missing.png"},
                "full_sha": "f" * 64}

        def empty_image(url, out, timeout=120):
            out.write_bytes(b"")
            return True

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "web_latest", return_value=info), \
                mock.patch.object(sync, "_curl_bytes", side_effect=empty_image), \
                mock.patch.object(sync, "replace_dir", wraps=sync.replace_dir) as replace:
            result = sync.sync_web_one(src, force=True)
        self.assertEqual(result, "failed")
        replace.assert_not_called()
        self.assertEqual((target / "old.md").read_bytes(), b"old mirror")
        self.assertEqual(src["pinned_version"], "1.0.0.1")

    def test_latest_sha_git_fallback_queries_heads_and_tags(self):
        head = "b" * 40
        response = subprocess.CompletedProcess([], 0, f"{head}\trefs/heads/main\n", "")
        with mock.patch.object(sync, "GH", ""), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", return_value=response) as run:
            self.assertEqual(sync.latest_sha("owner/repo", "main"), head)
        command = run.call_args.args[0]
        self.assertIn("refs/heads/main", command)
        self.assertIn("refs/tags/main", command)
        self.assertIn("refs/tags/main^{}", command)

    def test_latest_sha_rejects_ambiguous_head_and_tag_without_guessing(self):
        head, tag = "b" * 40, "c" * 40
        response = subprocess.CompletedProcess([], 0,
            f"{head}\trefs/heads/main\n{tag}\trefs/tags/main\n{tag}\trefs/tags/main^{{}}\n", "")
        with mock.patch.object(sync, "GH", ""), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", return_value=response):
            self.assertIsNone(sync.latest_sha("owner/repo", "main"))

    def test_latest_sha_unknown_uses_one_graphql_probe_for_head_and_tag(self):
        head = "b" * 40
        payload = {"data": {"repository": {
            "head": {"target": {"oid": head}}, "tag": None,
        }}}
        response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(sync, "GH", "gh"), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", return_value=response) as run:
            self.assertEqual(sync.latest_sha("owner/repo", "main"), head)
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["api", "graphql"])
        self.assertIn("head=refs/heads/main", command)
        self.assertIn("tag=refs/tags/main", command)

    def test_latest_sha_graphql_rejects_ambiguous_head_and_tag(self):
        head, tag = "b" * 40, "c" * 40
        payload = {"data": {"repository": {
            "head": {"target": {"oid": head}},
            "tag": {"target": {"oid": tag}},
        }}}
        response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(sync, "GH", "gh"), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", return_value=response) as run:
            self.assertIsNone(sync.latest_sha("owner/repo", "main"))
        self.assertEqual(run.call_count, 1)

    def test_latest_sha_graphql_peels_annotated_tag(self):
        tag, commit = "c" * 40, "d" * 40
        payload = {"data": {"repository": {
            "head": None,
            "tag": {"target": {"target": {"oid": commit}}},
        }}}
        response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(sync, "GH", "gh"), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", return_value=response) as run:
            self.assertEqual(sync.latest_sha("owner/repo", "v1"), commit)
        self.assertEqual(run.call_count, 1)

    def test_latest_sha_explicit_head_queries_only_head_ref(self):
        head = "b" * 40
        response = subprocess.CompletedProcess([], 0, f"{head}\trefs/heads/main\n", "")
        with mock.patch.object(sync, "GH", ""), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", return_value=response) as run:
            self.assertEqual(sync.latest_sha("owner/repo", "main", "head"), head)
        command = run.call_args.args[0]
        self.assertIn("refs/heads/main", command)
        self.assertFalse(any("refs/tags/" in arg for arg in command))

    def test_latest_sha_explicit_tag_queries_only_tag_and_peeled_ref(self):
        tag, commit = "c" * 40, "d" * 40
        response = subprocess.CompletedProcess([], 0,
            f"{tag}\trefs/tags/v1\n{commit}\trefs/tags/v1^{{}}\n", "")
        with mock.patch.object(sync, "GH", ""), \
                mock.patch.object(sync, "RETRIES", 1), \
                mock.patch.object(sync, "run", return_value=response) as run:
            self.assertEqual(sync.latest_sha("owner/repo", "v1", "tag"), commit)
        command = run.call_args.args[0]
        self.assertIn("refs/tags/v1", command)
        self.assertIn("refs/tags/v1^{}", command)
        self.assertFalse(any("refs/heads/" in arg for arg in command))

    def test_latest_sha_rejects_unknown_ref_kind_without_network(self):
        with mock.patch.object(sync, "run") as run:
            self.assertIsNone(sync.latest_sha("owner/repo", "main", "branch"))
        run.assert_not_called()

    def test_sync_one_passes_manifest_ref_kind_to_latest_sha(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "v1", "ref_kind": "tag",
               "local_dir": "snapshot", "subpath": "", "pinned_sha": "a" * 40}
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head) as latest, \
                mock.patch.object(sync, "index_tree", return_value=None), \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")), \
                mock.patch.object(sync, "replace_dir", return_value=True):
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "updated")
        latest.assert_called_once_with("owner/repo", "v1", "tag")

    def test_main_check_passes_manifest_ref_kind_to_latest_sha(self):
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "ref_kind": "head",
               "local_dir": "snapshot", "subpath": "", "pinned_sha": "a" * 40}
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"sources": [src]}), encoding="utf-8")
        with mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "latest_sha", return_value="a" * 40) as latest, \
                mock.patch.object(sys, "argv", ["sync_upstream.py", "--check"]):
            result = sync.main()
        self.assertEqual(result, 0)
        latest.assert_called_once_with("owner/repo", "main", "head")

    def test_main_check_probes_three_sources_concurrently_and_isolates_failure(self):
        import threading
        sources = [
            {"id": name, "repo": f"owner/{name}", "ref": "main",
             "local_dir": name, "pinned_sha": "a" * 40}
            for name in ("first", "second", "third")
        ]
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")
        barrier = threading.Barrier(3)
        probed = []

        def probe(repo, ref, ref_kind=None):
            barrier.wait(timeout=5)
            probed.append(repo)
            return None if repo == "owner/second" else "a" * 40

        with mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "latest_sha", side_effect=probe), \
                mock.patch.object(sys, "argv", ["sync_upstream.py", "--check"]):
            result = sync.main()
        self.assertEqual(result, 1)
        self.assertEqual(sorted(probed), ["owner/first", "owner/second", "owner/third"])

    def test_latest_sha_git_fallback_accepts_only_matching_successful_refs(self):
        head = "b" * 40
        cases = [
            (0, f"{'a' * 40}\trefs/heads/other\n{head}\trefs/heads/main\n", head),
            (1, f"{head}\trefs/heads/main\n", None),
            (0, f"{head}\trefs/heads/other\n", None),
            (0, "not-a-sha\trefs/heads/main\n", None),
        ]
        for returncode, stdout, expected in cases:
            response = subprocess.CompletedProcess([], returncode, stdout, "")
            with self.subTest(stdout=stdout, returncode=returncode), \
                    mock.patch.object(sync, "GH", ""), \
                    mock.patch.object(sync, "RETRIES", 1), \
                    mock.patch.object(sync, "run", return_value=response):
                self.assertEqual(sync.latest_sha("owner/repo", "main"), expected)

    def test_latest_sha_gh_timeout_or_oserror_falls_back_to_git(self):
        head = "b" * 40
        errors = [subprocess.TimeoutExpired("gh", 1), OSError("gh unavailable")]
        fallback = subprocess.CompletedProcess([], 0, f"{head}\trefs/heads/main\n", "")
        for error in errors:
            with self.subTest(error=error), \
                    mock.patch.object(sync, "GH", "gh"), \
                    mock.patch.object(sync, "RETRIES", 1), \
                    mock.patch.object(sync, "run", side_effect=[error, fallback]) as run:
                self.assertEqual(sync.latest_sha("owner/repo", "main"), head)
                self.assertEqual(run.call_count, 2)

    def test_gh_api_timeout_or_oserror_returns_none_after_retries(self):
        errors = [subprocess.TimeoutExpired("gh", 1), OSError("gh unavailable")]
        with mock.patch.object(sync, "GH", "gh"), \
                mock.patch.object(sync, "RETRIES", 2), \
                mock.patch.object(sync, "RETRY_SLEEP", 0), \
                mock.patch.object(sync, "run", side_effect=errors) as run:
            self.assertIsNone(sync._gh_api("repos/owner/repo/git/trees/main"))
        self.assertEqual(run.call_count, 2)

    def test_download_tarball_rejects_compressed_file_over_limit(self):
        def fake_download(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_bytes(b"123456")
            return subprocess.CompletedProcess([], 0, "200", "")

        with mock.patch.object(sync, "run", side_effect=fake_download), \
                mock.patch.object(sync, "MAX_TARBALL_BYTES", 5, create=True), \
                mock.patch.object(sync, "_gzip_complete") as inspect:
            self.assertEqual(sync.download_tarball("owner/repo", "a" * 40), (None, ""))
        inspect.assert_not_called()

    def test_gzip_complete_rejects_expanded_stream_over_limit(self):
        tarball = self.root / "gzip-limit.tar.gz"
        tarball.write_bytes(tar_bytes({"a.txt": b"a"}))
        with mock.patch.object(sync, "GZIP_MAX_UNCOMPRESSED", 10, create=True):
            self.assertFalse(sync._gzip_complete(tarball))

    def test_extractor_rejects_member_count_over_limit_and_cleans_output(self):
        archive = tar_bytes({"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})
        tarball = self.root / "members.tar.gz"
        tarball.write_bytes(archive)
        dest = self.root / "members-out"
        dest.mkdir()
        with mock.patch.object(sync, "EXTRACT_MAX_MEMBERS", 2, create=True):
            self.assertIsNone(sync._extract_tarball(tarball, dest, None))
        self.assertEqual(list(dest.iterdir()), [])

    def test_extractor_rejects_single_file_over_limit_and_cleans_output(self):
        archive = tar_bytes({"big.txt": b"12345"})
        tarball = self.root / "large-file.tar.gz"
        tarball.write_bytes(archive)
        dest = self.root / "large-file-out"
        dest.mkdir()
        with mock.patch.object(sync, "EXTRACT_MAX_FILE_SIZE", 4, create=True):
            self.assertIsNone(sync._extract_tarball(tarball, dest, None))
        self.assertEqual(list(dest.iterdir()), [])

    def test_extractor_rejects_total_expansion_over_limit_and_cleans_output(self):
        archive = tar_bytes({"a.txt": b"123", "b.txt": b"456"})
        tarball = self.root / "large-total.tar.gz"
        tarball.write_bytes(archive)
        dest = self.root / "large-total-out"
        dest.mkdir()
        with mock.patch.object(sync, "EXTRACT_MAX_TOTAL_SIZE", 5, create=True):
            self.assertIsNone(sync._extract_tarball(tarball, dest, None))
        self.assertEqual(list(dest.iterdir()), [])

    def test_extractor_rejects_excessive_compression_ratio(self):
        data = b"x" * 4096
        archive = tar_bytes({"compressible.txt": data})
        self.assertGreater(len(data) / len(archive), 10)
        tarball = self.root / "ratio.tar.gz"
        tarball.write_bytes(archive)
        dest = self.root / "ratio-out"
        dest.mkdir()
        with mock.patch.object(sync, "EXTRACT_MAX_COMPRESSION_RATIO", 10, create=True):
            self.assertIsNone(sync._extract_tarball(tarball, dest, None))
        self.assertEqual(list(dest.iterdir()), [])

    def test_extractor_cleans_partial_root_after_write_failure(self):
        archive = tar_bytes({"a.txt": b"a", "b.txt": b"b"})
        tarball = self.root / "write-failure.tar.gz"
        tarball.write_bytes(archive)
        dest = self.root / "write-failure-out"
        dest.mkdir()
        original_open = Path.open

        def fail_second_file(path, mode="r", *args, **kwargs):
            if path.name == "b.txt" and mode == "xb":
                raise OSError("simulated disk full")
            return original_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", fail_second_file):
            self.assertIsNone(sync._extract_tarball(tarball, dest, None))
        self.assertFalse((dest / "repo-sha").exists())

    def test_replace_dir_serializes_competing_commits(self):
        import threading
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stages = {}
        for name in ("a", "b"):
            stage = self.root / f"stage-{name}"
            stage.mkdir()
            (stage / f"{name}.txt").write_bytes(name.encode())
            stages[name] = stage
        first_inside = threading.Event()
        allow_first = threading.Event()
        second_inside = threading.Event()
        real_replace = os.replace
        results = {}

        def controlled_replace(source, destination):
            if Path(destination) != target:
                return real_replace(source, destination)
            if threading.current_thread().name == "first":
                first_inside.set()
                if not allow_first.wait(5):
                    raise RuntimeError("test coordination timed out")
            else:
                second_inside.set()
            return real_replace(source, destination)

        def worker(name):
            results[name] = sync.replace_dir(stages[name], target)

        with mock.patch.object(sync.os, "replace", side_effect=controlled_replace):
            first = threading.Thread(target=worker, args=("a",), name="first")
            second = threading.Thread(target=worker, args=("b",), name="second")
            first.start()
            self.assertTrue(first_inside.wait(5))
            second.start()
            second_entered_while_blocked = second_inside.wait(0.25)
            allow_first.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(second_entered_while_blocked)
        self.assertEqual(results, {"a": True, "b": True})
        self.assertEqual((target / "b.txt").read_bytes(), b"b")
        self.assertFalse((target / "a.txt").exists())

    def test_replace_dir_recovers_unique_backup_after_interrupted_commit(self):
        class SimulatedCrash(BaseException):
            pass

        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        first_stage = self.root / "stage-first"
        first_stage.mkdir()
        (first_stage / "interrupted.txt").write_bytes(b"partial")
        with mock.patch.object(sync.os, "replace", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                sync.replace_dir(first_stage, target)
        self.assertFalse(target.exists())
        self.assertEqual(len(list(self.root.glob(".snapshot.old-*"))), 1)

        second_stage = self.root / "stage-second"
        second_stage.mkdir()
        (second_stage / "new.txt").write_bytes(b"new")
        self.assertTrue(sync.replace_dir(second_stage, target))
        self.assertEqual((target / "new.txt").read_bytes(), b"new")
        self.assertFalse((target / "old.txt").exists())
        self.assertFalse(any(self.root.glob(".snapshot.old-*")))
        self.assertFalse(any(self.root.glob(".snapshot.new-*")))

    def test_atomic_write_uses_same_directory_temp_fsync_and_replace(self):
        path = self.root / "manifest.json"
        path.write_bytes(b"old")
        real_replace, real_fsync = os.replace, os.fsync
        observed = {}

        def checked_replace(source, destination):
            observed["source"] = Path(source)
            observed["destination"] = Path(destination)
            self.assertEqual(Path(source).parent, path.parent)
            self.assertNotEqual(Path(source), path)
            self.assertEqual(Path(source).read_bytes(), b"new")
            return real_replace(source, destination)

        with mock.patch.object(sync.os, "replace", side_effect=checked_replace) as replace, \
                mock.patch.object(sync.os, "fsync", wraps=real_fsync) as fsync:
            sync.atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual(observed["destination"], path)
        replace.assert_called_once()
        fsync.assert_called()
        self.assertFalse(any(p.name.startswith(".manifest.json.") for p in self.root.iterdir()))

    def test_atomic_write_fsyncs_parent_directory_after_replace(self):
        path = self.root / "manifest.json"
        path.write_bytes(b"old")
        directory_fd = 10_000_000
        observed = []
        real_open, real_fsync, real_close = os.open, os.fsync, os.close

        def checked_open(file, flags, *args, **kwargs):
            if Path(file) == path.parent and flags & getattr(os, "O_DIRECTORY", 0):
                self.assertEqual(path.read_bytes(), b"new")
                observed.append("open-parent")
                return directory_fd
            return real_open(file, flags, *args, **kwargs)

        def checked_fsync(fd):
            if fd == directory_fd:
                observed.append("fsync-parent")
                return None
            return real_fsync(fd)

        def checked_close(fd):
            if fd == directory_fd:
                observed.append("close-parent")
                return None
            return real_close(fd)

        with mock.patch.object(sync.sys, "platform", "linux"), \
                mock.patch.object(sync.os, "O_DIRECTORY", 0x10000, create=True), \
                mock.patch.object(sync.os, "open", side_effect=checked_open), \
                mock.patch.object(sync.os, "fsync", side_effect=checked_fsync), \
                mock.patch.object(sync.os, "close", side_effect=checked_close):
            sync.atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual(observed, ["open-parent", "fsync-parent", "close-parent"])

    def test_atomic_write_windows_explicitly_warns_parent_fsync_is_unavailable(self):
        path = self.root / "manifest.json"
        path.write_bytes(b"old")
        with mock.patch.object(sync.sys, "platform", "win32"), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sync.atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"new")
        self.assertTrue(any(issubclass(item.category, RuntimeWarning)
                            and "Windows" in str(item.message)
                            and "父目录" in str(item.message)
                            and "无法保证" in str(item.message) for item in caught))

    def test_atomic_write_warns_when_posix_parent_fsync_fails(self):
        path = self.root / "manifest.json"
        path.write_bytes(b"old")
        real_open = os.open

        def fail_parent_open(file, flags, *args, **kwargs):
            if Path(file) == path.parent and flags & getattr(os, "O_DIRECTORY", 0):
                raise OSError("simulated directory fsync unavailable")
            return real_open(file, flags, *args, **kwargs)

        with mock.patch.object(sync.sys, "platform", "linux"), \
                mock.patch.object(sync.os, "O_DIRECTORY", 0x10000, create=True), \
                mock.patch.object(sync.os, "open", side_effect=fail_parent_open), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sync.atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"new")
        self.assertTrue(any(issubclass(item.category, RuntimeWarning)
                            and "父目录 fsync 失败" in str(item.message) for item in caught))

    def test_atomic_write_replace_failure_preserves_original_and_cleans_temp(self):
        path = self.root / "manifest.json"
        path.write_bytes(b"old")
        with mock.patch.object(sync.os, "replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                sync.atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"old")
        self.assertFalse(any(p.name.startswith(".manifest.json.") for p in self.root.iterdir()))

    def test_commit_source_rereads_and_merges_latest_manifest(self):
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "pinned_sha": "a" * 40}
        other = {"id": "other", "repo": "owner/other", "ref": "main", "local_dir": "other",
                 "pinned_sha": "d" * 40, "last_synced": "2026-01-01"}
        stale_cfg = {"sources": [src, json.loads(json.dumps(other))]}
        manifest = self.root / "manifest.json"
        latest_other = {**other, "pinned_sha": "e" * 40, "last_synced": "2026-11-18",
                        "external_field": "preserve me"}
        manifest.write_text(json.dumps({"sources": [src, latest_other]}, ensure_ascii=False),
                            encoding="utf-8")
        with mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", stale_cfg):
            sync._commit_source(src, {"pinned_sha": "b" * 40})
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
        self.assertEqual(payload["sources"][0]["pinned_sha"], "b" * 40)
        self.assertEqual(payload["sources"][1], latest_other)
        self.assertEqual(src["pinned_sha"], "b" * 40)

    def test_concurrent_sync_one_serializes_manifest_and_synclog_without_losing_pins(self):
        import threading
        manifest = self.root / "manifest.json"
        synclog = self.root / "SYNCLOG.md"
        sources = [
            {"id": name, "repo": f"owner/{name}", "ref": "main", "local_dir": name,
             "subpath": "", "pinned_sha": "a" * 40}
            for name in ("first", "second")
        ]
        stages = {}
        for src in sources:
            target = self.root / src["local_dir"]
            target.mkdir()
            (target / "old.txt").write_bytes(b"old")
            stage = self.root / f"stage-{src['id']}"
            stage.mkdir()
            (stage / "new.txt").write_bytes(b"new")
            stages[src["repo"]] = stage
        manifest.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
        first_atomic_entered = threading.Event()
        allow_first_atomic = threading.Event()
        first_log_entered = threading.Event()
        allow_first_log = threading.Event()
        second_atomic_entered = threading.Event()
        real_atomic, real_open = sync.atomic_write, open
        results, errors = {}, {}

        def controlled_atomic(path, data):
            if Path(path) == manifest:
                if threading.current_thread().name == "first":
                    first_atomic_entered.set()
                    if not allow_first_atomic.wait(5):
                        raise RuntimeError("test coordination timed out")
                else:
                    second_atomic_entered.set()
            return real_atomic(path, data)

        def controlled_open(path, mode="r", *args, **kwargs):
            if Path(path) == synclog and mode == "a" and threading.current_thread().name == "first":
                first_log_entered.set()
                if not allow_first_log.wait(5):
                    raise RuntimeError("test coordination timed out")
            return real_open(path, mode, *args, **kwargs)

        def worker(src):
            try:
                results[src["id"]] = sync.sync_one(src, force=True)
            except BaseException as exc:
                errors[src["id"]] = exc

        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "SYNCLOG", synclog), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": sources}), \
                mock.patch.object(sync, "latest_sha", side_effect=lambda repo, ref, ref_kind=None: "b" * 40), \
                mock.patch.object(sync, "index_tree", return_value=None), \
                mock.patch.object(sync, "download_tarball", side_effect=lambda repo, sha, **kw: (stages[repo], "codeload")), \
                mock.patch.object(sync, "replace_dir", return_value=True), \
                mock.patch.object(sync, "atomic_write", side_effect=controlled_atomic), \
                mock.patch("builtins.open", side_effect=controlled_open):
            first = threading.Thread(target=worker, args=(sources[0],), name="first")
            second = threading.Thread(target=worker, args=(sources[1],), name="second")
            first.start()
            try:
                self.assertTrue(first_atomic_entered.wait(5))
                second.start()
                self.assertFalse(second_atomic_entered.wait(0.25))
                allow_first_atomic.set()
                self.assertTrue(first_log_entered.wait(5))
                self.assertFalse(second_atomic_entered.wait(0.25))
            finally:
                allow_first_atomic.set()
                allow_first_log.set()
                first.join(5)
                second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, {})
        self.assertEqual(results, {"first": "updated", "second": "updated"})
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
        self.assertEqual({item["id"]: item["pinned_sha"] for item in payload["sources"]},
                         {"first": "b" * 40, "second": "b" * 40})
        log = synclog.read_text(encoding="utf-8")
        self.assertIn("`first`", log)
        self.assertIn("`second`", log)

    def test_sync_one_uses_atomic_manifest_write(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "", "pinned_sha": "a" * 40}
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "a.txt").write_bytes(b"new")
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "index_tree", return_value=None), \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")), \
                mock.patch.object(sync, "atomic_write", wraps=sync.atomic_write) as atomic:
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "updated")
        atomic.assert_called_once()
        self.assertEqual(atomic.call_args.args[0], self.root / "manifest.json")
        self.assertIn(head, atomic.call_args.args[1].decode("utf-8-sig"))

    def test_sync_one_manifest_failure_returns_failed_without_advancing_pin(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "", "pinned_sha": "a" * 40}
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "a.txt").write_bytes(b"new")
        manifest = self.root / "manifest.json"
        original = json.dumps({"sources": [src]}, ensure_ascii=False).encode("utf-8")
        manifest.write_bytes(original)
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "index_tree", return_value=None), \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")), \
                mock.patch.object(sync, "atomic_write", side_effect=OSError("simulated manifest failure")) as atomic:
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "failed")
        atomic.assert_called_once()
        self.assertEqual(src["pinned_sha"], "a" * 40)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertFalse((self.root / "SYNCLOG.md").exists())

    def test_web_manifest_failure_returns_failed_without_advancing_pin(self):
        src = self._web_src()
        info = {"pages": {"new.md": "# New\n"}, "top_version": "1.2.3.4",
                "page_names": {"new.md"}, "images": set(), "full_sha": "f" * 64}
        manifest = self.root / "manifest.json"
        original = json.dumps({"sources": [src]}, ensure_ascii=False).encode("utf-8")
        manifest.write_bytes(original)
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", manifest), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "web_latest", return_value=info), \
                mock.patch.object(sync, "atomic_write", side_effect=OSError("simulated manifest failure")) as atomic:
            result = sync.sync_web_one(src, force=True)
        self.assertEqual(result, "failed")
        atomic.assert_called_once()
        self.assertEqual(src["pinned_version"], "1.0.0.1")
        self.assertEqual(src["pinned_full_sha"], "0" * 64)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertFalse((self.root / "SYNCLOG.md").exists())

    def test_replace_dir_uses_unique_backup_and_preserves_existing_old_dir(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        legacy_backup = target.with_name("snapshot.old")
        legacy_backup.mkdir()
        (legacy_backup / "sentinel.txt").write_bytes(b"do not delete")
        original_rename = Path.rename
        destinations = []

        def record_rename(path, destination):
            destinations.append((path, destination))
            return original_rename(path, destination)

        with mock.patch.object(Path, "rename", record_rename):
            ok = sync.replace_dir(stage, target)
        self.assertTrue(ok)
        self.assertEqual((legacy_backup / "sentinel.txt").read_bytes(), b"do not delete")
        backup = next(dst for src, dst in destinations if src == target)
        self.assertNotEqual(backup, legacy_backup)
        self.assertTrue(backup.name.startswith(".snapshot.old-"))
        self.assertEqual(backup.parent, target.parent)
        self.assertFalse(backup.exists())
        self.assertEqual((target / "new.txt").read_bytes(), b"new")

    def test_replace_dir_copy_failure_happens_before_target_rename(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        original_rename = Path.rename
        rename_calls = []

        def record_rename(path, destination):
            rename_calls.append((path, destination))
            return original_rename(path, destination)

        with mock.patch.object(sync.shutil, "copytree", side_effect=OSError("simulated copy failure")), \
                mock.patch.object(Path, "rename", record_rename):
            ok = sync.replace_dir(stage, target)
        self.assertFalse(ok)
        self.assertEqual(rename_calls, [])
        self.assertEqual((target / "old.txt").read_bytes(), b"old")
        self.assertFalse((target / "new.txt").exists())

    def test_replace_dir_commit_failure_rolls_back_to_unique_backup(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        real_replace = os.replace

        def fail_commit(source, destination):
            if Path(destination) == target:
                raise OSError("simulated commit failure")
            return real_replace(source, destination)

        with mock.patch.object(sync.os, "replace", side_effect=fail_commit):
            ok = sync.replace_dir(stage, target)
        self.assertFalse(ok)
        self.assertEqual((target / "old.txt").read_bytes(), b"old")
        self.assertFalse((target / "new.txt").exists())
        self.assertFalse(any(target.parent.glob(".snapshot.old-*")))
        self.assertFalse(any(target.parent.glob(".snapshot.new-*")))

    def test_replace_dir_rollback_failure_raises_with_backup_recovery_clue(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "old.txt").write_bytes(b"old")
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "new.txt").write_bytes(b"new")
        real_rename = Path.rename
        backup = {}

        def fail_rollback(path, destination):
            if path == target:
                backup["path"] = destination
                return real_rename(path, destination)
            if path == backup.get("path"):
                raise PermissionError("simulated rollback failure")
            return real_rename(path, destination)

        def fail_commit(source, destination):
            raise OSError("simulated commit failure")

        with mock.patch.object(Path, "rename", fail_rollback), \
                mock.patch.object(sync.os, "replace", side_effect=fail_commit):
            with self.assertRaises(sync.ReplaceError) as raised:
                sync.replace_dir(stage, target)
        self.assertEqual(raised.exception.backup, backup["path"])
        self.assertEqual((backup["path"] / "old.txt").read_bytes(), b"old")
        self.assertIn("回滚失败", str(raised.exception))
        self.assertIn(str(backup["path"]), str(raised.exception))

    def test_incremental_propagates_replace_error_without_touching_target(self):
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.py").write_bytes(b"old!")
        src = {"repo": "owner/repo", "local_dir": "snapshot", "subpath": ""}
        old_sha, new_sha = "a" * 40, "b" * 40
        old_tree = {"a.py": entry(b"old!")}
        new_tree = {"a.py": entry(b"new!")}
        backup = self.root / "snapshot-recovery"
        backup.mkdir()
        (backup / "a.py").write_bytes(b"old!")
        failure = sync.ReplaceError("simulated rollback failure", backup=backup, target=target)
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "a.py", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=[new_tree, old_tree]), \
                mock.patch.object(sync, "fetch_raw", return_value=b"new!"), \
                mock.patch.object(sync, "replace_dir", side_effect=failure):
            with self.assertRaises(sync.ReplaceError) as raised:
                sync.sync_incremental(src, old_sha, new_sha)
        self.assertIs(raised.exception, failure)
        self.assertEqual(raised.exception.backup, backup)
        self.assertEqual((target / "a.py").read_bytes(), b"old!")
        self.assertEqual((backup / "a.py").read_bytes(), b"old!")

    def test_sync_one_incremental_replace_error_is_fatal_without_second_replace(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "", "pinned_sha": "a" * 40}
        target = self.root / "snapshot"
        target.mkdir()
        (target / "a.txt").write_bytes(b"old")
        backup = self.root / ".snapshot.old-recovery"
        backup.mkdir()
        (backup / "a.txt").write_bytes(b"old")
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "a.txt").write_bytes(b"new")
        old_tree = {"a.txt": entry(b"old")}
        new_tree = {"a.txt": entry(b"new")}
        failure = sync.ReplaceError("simulated rollback failure", backup=backup, target=target)
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "compare_changes", return_value=[
                    {"path": "a.txt", "status": "modified", "previous_path": ""}]), \
                mock.patch.object(sync, "index_tree", side_effect=lambda repo, sha: new_tree if sha == head else old_tree), \
                mock.patch.object(sync, "fetch_raw", return_value=b"new"), \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")) as download, \
                mock.patch.object(sync, "replace_dir", side_effect=[failure, True]) as replace:
            result = sync.sync_one(src)
        self.assertEqual(result, "failed")
        replace.assert_called_once()
        download.assert_not_called()
        self.assertEqual(src["pinned_sha"], "a" * 40)
        self.assertEqual((target / "a.txt").read_bytes(), b"old")
        self.assertEqual((backup / "a.txt").read_bytes(), b"old")
        self.assertFalse((self.root / "manifest.json").exists())
        self.assertFalse((self.root / "SYNCLOG.md").exists())

    def test_sync_one_reports_rollback_failure_without_advancing_pin(self):
        head = "b" * 40
        src = {"id": "demo", "repo": "owner/repo", "ref": "main", "local_dir": "snapshot",
               "subpath": "", "pinned_sha": "a" * 40}
        stage = self.root / "repo-root"
        stage.mkdir()
        (stage / "a.txt").write_bytes(b"new")
        backup = self.root / "snapshot-recovery"
        failure = sync.ReplaceError("simulated rollback failure", backup=backup,
                                    target=self.root / "snapshot")
        with mock.patch.object(sync, "SKILL_ROOT", self.root), \
                mock.patch.object(sync, "MANIFEST", self.root / "manifest.json"), \
                mock.patch.object(sync, "SYNCLOG", self.root / "SYNCLOG.md"), \
                mock.patch.object(sync, "MANIFEST_CFG", {"sources": [src]}), \
                mock.patch.object(sync, "latest_sha", return_value=head), \
                mock.patch.object(sync, "index_tree", return_value=None), \
                mock.patch.object(sync, "download_tarball", return_value=(stage, "codeload")), \
                mock.patch.object(sync, "replace_dir", side_effect=failure):
            result = sync.sync_one(src, force=True)
        self.assertEqual(result, "failed")
        self.assertEqual(src["pinned_sha"], "a" * 40)
        self.assertFalse((self.root / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
