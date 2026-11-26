#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlwings 技能上游同步脚本。

用法:
    python scripts/sync_upstream.py --check          # 仅检测更新，不替换
    python scripts/sync_upstream.py                  # 检测并同步有更新的上游
    python scripts/sync_upstream.py --force <id> [<id> ...]   # 只同步所列 source，且忽略 sha 比对强制重拉

机制:
  1. 读 manifest.json 中每路上游的 repo/ref/subpath/local_dir/pinned_sha
  2. 经 gh API（多候选探测 gh.exe，降级 git ls-remote）查询 ref 最新 commit；带重试；无变化则跳过
  3. 按通道链下载 tarball：codeload 直连 → archive 直连；仅有可验证 HEAD tree 时才追加 ghfast 代理
  4. 同卷候选目录完整校验后，以唯一备份 + os.replace 提交；失败回滚，中断则下次持锁恢复
  5. manifest 经同目录临时文件 + fsync + os.replace 原子更新，并追加 SYNCLOG.md

约束:
  - 持久技能内容只写 local_dir、manifest.json、SYNCLOG.md；stage/唯一备份仅用于恢复，系统临时目录锁文件用于并发互斥，绝不触碰 SKILL.md / references/ 等自研文件。
  - local_dir 一律是固定目录名（xlwings、xlwings-server），不随上游版本号改变。要切换跟踪点只改
    manifest.json 的 ref：跟最新正式版填 tag（如 0.37.4），跟开发线填 main。
  - 上游内容变化后，自研文档里引用的行号与代码片段需人工复核（脚本不做这件事）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
import warnings
import zlib
from contextlib import contextmanager
from datetime import date
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import web_mirror  # noqa: E402  （xlwings-lite 官网镜像重建的纯逻辑层）

MANIFEST = SKILL_ROOT / "manifest.json"
SYNCLOG = SKILL_ROOT / "SYNCLOG.md"
TIMEOUT = 120
DOWNLOAD_TIMEOUT = 900
RETRIES = 3
RETRY_SLEEP = 3

# 解包资源护栏：拒绝成员过多、单文件/总展开过大或压缩比异常的归档。
EXTRACT_MAX_MEMBERS = 20_000
EXTRACT_MAX_FILE_SIZE = 128 * 1024 * 1024
EXTRACT_MAX_TOTAL_SIZE = 512 * 1024 * 1024
EXTRACT_MAX_COMPRESSION_RATIO = 200
MAX_HTTP_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_TARBALL_BYTES = 512 * 1024 * 1024
# tar 头/填充也占用解压流预算；给每个成员预留 4KiB 元数据空间。
GZIP_MAX_UNCOMPRESSED = EXTRACT_MAX_TOTAL_SIZE + EXTRACT_MAX_MEMBERS * 4096 + 1024 * 1024

# 下载通道降级链：codeload 直连 → archive 直连 → ghfast 代理 archive。
# 本机到 GitHub 直连时通时断（见 SYNCLOG 2026-09-24），通道链保证弱网下仍可同步。
# 但 tree 不可验证时只允许官方 HTTPS，不能信任第三方代理内容。
OFFICIAL_DOWNLOAD_CHANNELS = ("codeload", "archive")
DOWNLOAD_CHANNELS = (
    "codeload",   # https://codeload.github.com/{repo}/tar.gz/{ref_path}
    "archive",    # https://github.com/{repo}/archive/{ref_path}.tar.gz
    "ghfast",     # https://ghfast.top/https://github.com/{repo}/archive/{ref_path}.tar.gz
)


def _find_gh() -> str:
    """多候选探测 gh.exe：PATH 命中且真实存在 → Program Files → 各用户 LOCALAPPDATA。"""
    candidates = [shutil.which("gh")]
    candidates.append(str(Path(r"C:\Program Files\GitHub CLI\gh.exe")))
    drive = os.environ.get("SystemDrive", "C:")
    candidates += [str(p) for p in Path(f"{drive}\\Users").glob(
        r"*\AppData\Local\Programs\gh\bin\gh.exe")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


GH = _find_gh()
_REPLACE_THREAD_LOCK = threading.RLock()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=kw.pop("timeout", TIMEOUT), **kw)


def _fsync_parent_directory(path: Path) -> None:
    """替换后尽力同步目录项；平台不支持或失败时明确告警，不伪报持久性。"""
    if sys.platform == "win32":
        warnings.warn("Windows 无法安全 fsync 父目录；文件内容已同步，但目录项崩溃持久性无法保证",
                      RuntimeWarning, stacklevel=3)
        return
    directory_fd = None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(directory_fd)
    except OSError as exc:
        warnings.warn(f"父目录 fsync 失败，目录项持久性无法保证：{exc}",
                      RuntimeWarning, stacklevel=3)
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError as exc:
                warnings.warn(f"父目录关闭失败，目录项持久性无法保证：{exc}",
                              RuntimeWarning, stacklevel=3)


def atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + flush/fsync + os.replace + 父目录 fsync；失败时清理临时文件。"""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        _fsync_parent_directory(path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _append_synclog_unlocked(line: str) -> None:
    """调用方必须持有 manifest 全局锁，保证日志行不会与其他提交交错。"""
    with open(SYNCLOG, "a", encoding="utf-8", newline="\n") as stream:
        if stream.tell() == 0:
            stream.write("# xlwings 技能上游同步日志\n\n| 日期 | 来源 | 方式 | 新 commit | 规模 |\n|---|---|---|---|---|\n")
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _commit_source(src: dict, updates: dict, log_line: str | None = None) -> None:
    """全局锁内重读 manifest，按 source id 合并后原子写入，并在同一锁内追加日志。

    不使用进程启动时的旧 MANIFEST_CFG 整份覆盖磁盘；其他 source 的最新 pin 和外部字段
    都得以保留。manifest 写入失败时 src/MANIFEST_CFG 不推进；日志失败只告警，因为此时
    manifest 已提交，不能伪报 pin 未推进。
    """
    global MANIFEST_CFG
    with _replacement_lock(MANIFEST):
        if MANIFEST.exists():
            latest = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
        elif MANIFEST_CFG is not None:
            latest = json.loads(json.dumps(MANIFEST_CFG))
        else:
            raise ValueError("manifest 不存在且没有可用的内存配置")
        if not isinstance(latest, dict) or not isinstance(latest.get("sources"), list):
            raise ValueError("manifest 结构非法")
        matches = [item for item in latest["sources"]
                   if isinstance(item, dict) and item.get("id") == src.get("id")]
        if len(matches) != 1:
            raise ValueError(f"manifest 中 source id 不唯一或缺失：{src.get('id')}")
        matches[0].update(updates)
        payload = json.dumps(latest, ensure_ascii=False, indent=2).encode("utf-8-sig")
        atomic_write(MANIFEST, payload)
        src.update(updates)
        MANIFEST_CFG = latest
        if log_line:
            try:
                _append_synclog_unlocked(log_line)
            except OSError as exc:
                print(f"  [{src['id']}] manifest 已提交但 SYNCLOG 写入失败：{exc}")


def _graphql_commit_oid(target) -> str | None:
    for _ in range(8):
        if not isinstance(target, dict):
            return None
        oid = target.get("oid")
        if oid is not None:
            return oid if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{40}", oid) else None
        target = target.get("target")
    return None


def _latest_sha_graphql(repo: str, ref: str) -> tuple[bool, str | None]:
    """一次 GraphQL 请求同时解析 heads/tags；返回 (是否得到权威结果, commit)。"""
    if not GH or repo.count("/") != 1:
        return False, None
    owner, name = repo.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$head:String!,$tag:String!){"
        "repository(owner:$owner,name:$name){"
        "head:ref(qualifiedName:$head){target{... on Commit{oid}}}"
        "tag:ref(qualifiedName:$tag){target{... on Commit{oid} "
        "... on Tag{target{... on Commit{oid}}}}}}}"
    )
    try:
        result = run([
            GH, "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"name={name}",
            "-F", f"head=refs/heads/{ref}", "-F", f"tag=refs/tags/{ref}",
        ])
        if result.returncode != 0:
            return False, None
        payload = json.loads(result.stdout)
        repository = payload["data"]["repository"]
        if not isinstance(repository, dict):
            return False, None
        head_ref, tag_ref = repository.get("head"), repository.get("tag")
        if any(item is not None and not isinstance(item, dict) for item in (head_ref, tag_ref)):
            return False, None
        head = _graphql_commit_oid(head_ref.get("target")) if head_ref else None
        tag = _graphql_commit_oid(tag_ref.get("target")) if tag_ref else None
        if head and tag:
            return True, None
        return True, head or tag
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return False, None


def latest_sha(repo: str, ref: str, ref_kind: str | None = None) -> str | None:
    """解析最新 commit；ref_kind=head/tag 时精确查询，否则发现 heads/tags 同名即失败。

    未声明种类时用单次 GraphQL 同时观察 heads/tags，避免额外 git 往返；显式种类可使用
    完整 ref 的 GH API，或只查询对应的 ls-remote 模式。annotated tag 优先返回 peeled commit SHA。
    """
    if ref_kind not in (None, "head", "tag"):
        return None
    head_ref, tag_ref = f"refs/heads/{ref}", f"refs/tags/{ref}"
    peeled_ref = f"{tag_ref}^{{}}"
    exact_ref = head_ref if ref_kind == "head" else tag_ref if ref_kind == "tag" else None
    patterns = ([exact_ref] if ref_kind == "head" else
                [tag_ref, peeled_ref] if ref_kind == "tag" else [head_ref, tag_ref, peeled_ref])
    for attempt in range(RETRIES):
        if exact_ref is None:
            resolved, candidate = _latest_sha_graphql(repo, ref)
            if resolved:
                return candidate
        elif GH:
            try:
                r = run([GH, "api", f"repos/{repo}/commits/{exact_ref}", "--jq", ".sha"])
                candidate = r.stdout.strip()
                if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", candidate):
                    return candidate
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            r = run(["git", "ls-remote", f"https://github.com/{repo}.git", *patterns])
            if r.returncode == 0:
                refs = {}
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if len(parts) != 2 or parts[1] not in patterns:
                        continue
                    if not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                        return None
                    if parts[1] in refs and refs[parts[1]] != parts[0]:
                        return None
                    refs[parts[1]] = parts[0]
                if ref_kind == "head":
                    return refs.get(head_ref)
                if ref_kind == "tag":
                    return refs.get(peeled_ref) or refs.get(tag_ref)
                has_head, has_tag = head_ref in refs, tag_ref in refs or peeled_ref in refs
                if has_head and has_tag:
                    return None
                if has_tag:
                    return refs.get(peeled_ref) or refs.get(tag_ref)
                if has_head:
                    return refs[head_ref]
        except Exception:
            pass
        if attempt < RETRIES - 1:
            import time
            time.sleep(RETRY_SLEEP)
    return None


def _gzip_complete(path: Path) -> bool:
    """在资源预算内读取整个 gzip 流，验证 CRC、长度与结尾。"""
    total = 0
    try:
        with gzip.open(path, "rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > GZIP_MAX_UNCOMPRESSED:
                    return False
    except (OSError, EOFError, zlib.error):
        return False
    return True


def _extract_tarball(tarball: Path, tmp: Path, expected_files: int | None) -> Path | None:
    """在资源预算内安全解包单一根目录，拒绝链接、特殊成员和路径穿越。"""
    root = None
    root_created = False
    success = False
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            roots, seen, files, dirs, normalized = set(), set(), set(), set(), []
            total_size = 0
            for member in archive:
                if len(normalized) >= EXTRACT_MAX_MEMBERS:
                    return None
                if not (member.isfile() or member.isdir()) or member.size < 0:
                    return None
                if member.isfile() and member.size > EXTRACT_MAX_FILE_SIZE:
                    return None
                total_size += member.size
                if total_size > EXTRACT_MAX_TOTAL_SIZE:
                    return None
                name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
                if not _safe_relpath(name) or name in seen:
                    return None
                parts = name.split("/")
                roots.add(parts[0])
                if len(roots) != 1 or (len(parts) == 1 and not member.isdir()):
                    return None
                for i in range(1, len(parts)):
                    if "/".join(parts[:i]) in files:
                        return None
                if member.isfile():
                    if name in dirs:
                        return None
                    files.add(name)
                else:
                    dirs.add(name)
                seen.add(name)
                normalized.append((member, parts))
            if not normalized or len(roots) != 1:
                return None
            if total_size > EXTRACT_MAX_COMPRESSION_RATIO * max(tarball.stat().st_size, 1):
                return None
            if expected_files is not None and len(files) != expected_files:
                return None
            root = tmp / next(iter(roots))
            root.mkdir()
            root_created = True
            total_written = 0
            for member, parts in normalized:
                dest = root.joinpath(*parts[1:])
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    return None
                copied = 0
                with source, dest.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        total_written += len(chunk)
                        if (copied > member.size or copied > EXTRACT_MAX_FILE_SIZE
                                or total_written > EXTRACT_MAX_TOTAL_SIZE):
                            return None
                        output.write(chunk)
                if copied != member.size:
                    return None
                dest.chmod(member.mode & 0o777)
            if count_files(root) != len(files):
                return None
            success = True
            return root
    except (OSError, EOFError, tarfile.TarError, ValueError):
        return None
    finally:
        if root_created and not success:
            shutil.rmtree(root, ignore_errors=True)


def download_tarball(repo: str, sha: str, expected_files: int | None = None,
                     *, tree_verified: bool = False) -> tuple[Path | None, str]:
    """下载已检测 SHA 的 tarball 并安全解包；tree_verified 才允许第三方代理通道。"""
    if expected_files is not None and (type(expected_files) is not int or expected_files < 0):
        return None, ""
    channels = DOWNLOAD_CHANNELS if tree_verified is True else OFFICIAL_DOWNLOAD_CHANNELS
    tmp = Path(tempfile.mkdtemp(prefix="xlwings_up_"))
    tarball = tmp / "src.tar.gz"
    urls = {
        "codeload": f"https://codeload.github.com/{repo}/tar.gz/{sha}",
        "archive": f"https://github.com/{repo}/archive/{sha}.tar.gz",
        "ghfast": f"https://ghfast.top/https://github.com/{repo}/archive/{sha}.tar.gz",
    }
    for channel in channels:
        try:
            r = run(["curl.exe", "-sL", "-o", str(tarball), urls[channel],
                     "-w", "%{http_code}", "--max-filesize", str(MAX_TARBALL_BYTES)],
                    timeout=DOWNLOAD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            print(f"  下载失败或超时（{DOWNLOAD_TIMEOUT}s）：{urls[channel]}")
            tarball.unlink(missing_ok=True)
            continue
        if r.returncode == 0 and tarball.exists() and r.stdout.strip() == "200":
            try:
                too_large = tarball.stat().st_size > MAX_TARBALL_BYTES
            except OSError:
                too_large = True
            if too_large:
                tarball.unlink(missing_ok=True)
                continue
            if not _gzip_complete(tarball):
                tarball.unlink(missing_ok=True)
                continue
            root = _extract_tarball(tarball, tmp, expected_files)
            if root is not None:
                return root, channel
            for child in tmp.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        tarball.unlink(missing_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return None, ""


# web 源抓取：用 urllib 而非 curl.exe。本机 schannel curl 会因 CRL 吊销检查失败
# （CRYPT_E_REVOCATION，代理/直连都绕不过）；urllib 保留默认证书校验（不走那条失败的
# CRL 路径），既可靠又不像 curl --insecure 那样放弃证书校验（镜像要进公开仓库，完整性重要）。
_UA = {"User-Agent": "Mozilla/5.0 (xlwings-skill sync)"}
_IMAGE_CONTENT_TYPES = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg",
    "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
    "image/tiff": "tiff", "image/avif": "avif", "image/svg+xml": "svg",
}
_IMAGE_EXTENSIONS = {
    "png": {".png"}, "jpeg": {".jpg", ".jpeg"}, "gif": {".gif"},
    "webp": {".webp"}, "bmp": {".bmp"}, "ico": {".ico"},
    "tiff": {".tif", ".tiff"}, "avif": {".avif"}, "svg": {".svg"},
}


def _image_format(data: bytes) -> str | None:
    """按常见图片签名识别格式；SVG 要求首个实质元素是 svg 根节点。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "avif"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    while head.startswith((b"<?xml", b"<!--")):
        end_marker = b"?>" if head.startswith(b"<?xml") else b"-->"
        end = head.find(end_marker)
        if end < 0:
            return None
        head = head[end + len(end_marker):].lstrip(b" \t\r\n")
    return "svg" if re.match(br"<svg(?:\s|>)", head, re.IGNORECASE) else None


def _image_name_matches_format(name: str, image_format: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_EXTENSIONS.get(image_format, set())


def _http_get(url: str, timeout: int, *, image: bool = False,
              allow_empty: bool = False) -> bytes | None:
    import ssl, urllib.parse, urllib.request, time
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if urllib.parse.urlsplit(final_url).scheme.lower() != "https":
                    raise ValueError("响应 URL 不是 HTTPS")
                declared = response.headers.get("Content-Length")
                expected = None
                if isinstance(declared, str):
                    try:
                        expected = int(declared)
                    except ValueError as exc:
                        raise ValueError("Content-Length 非法") from exc
                    if expected < 0 or expected > MAX_HTTP_DOWNLOAD_BYTES:
                        raise ValueError("响应超过下载上限")
                media_type = ""
                if image:
                    media_type = response.headers.get_content_type().split(";", 1)[0].strip().lower()
                    if media_type not in _IMAGE_CONTENT_TYPES:
                        raise ValueError("响应不是允许的图片 Content-Type")
                chunks, total = [], 0
                while True:
                    chunk = response.read(min(1024 * 1024, MAX_HTTP_DOWNLOAD_BYTES + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_HTTP_DOWNLOAD_BYTES:
                        raise ValueError("响应超过下载上限")
                    chunks.append(chunk)
                if expected is not None and total != expected:
                    raise ValueError("Content-Length 与实际长度不符")
                data = b"".join(chunks)
            if image and _image_format(data) != _IMAGE_CONTENT_TYPES[media_type]:
                raise ValueError("图片 Content-Type 与内容签名不符")
            if data or allow_empty:
                return data
        except Exception:
            if attempt + 1 < RETRIES:
                time.sleep(RETRY_SLEEP)
    return None

def _curl_text(url: str, timeout: int = 120) -> str | None:
    """抓取文本内容（web 源专用）。"""
    data = _http_get(url, timeout)
    return data.decode("utf-8", errors="replace") if data else None

def _image_path_is_safe(path: Path) -> bool:
    """图片写入路径本身及其直接父目录都不得是符号链接。"""
    return not path.is_symlink() and not path.parent.is_symlink()


def _curl_bytes(url: str, out: Path, timeout: int = 120) -> bool:
    """抓取二进制（图片）到 out；下载前后都拒绝跟随符号链接。"""
    if not _image_path_is_safe(out):
        return False
    data = _http_get(url, timeout, image=True)
    if not data or not _image_path_is_safe(out):
        return False
    image_format = _image_format(data)
    if image_format is None or not _image_name_matches_format(out.name, image_format):
        return False
    created = False
    try:
        # 排他创建同时防止检查后被预置的普通文件或链接覆盖。
        with out.open("xb") as stream:
            created = True
            stream.write(data)
    except OSError:
        if created:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    return True

def git_blob_sha(data: bytes) -> str:
    """计算 Git blob 对象 SHA-1（含对象头，不含换行转换）。"""
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def count_files(p: Path) -> int:
    return sum(1 for _ in p.rglob("*") if _.is_file())


def _keep_rel(target: Path, keep) -> set:
    """展开 keep 白名单（相对 local_dir 的路径片段）为 target 下需保留的文件相对路径集合。

    keep 项按「路径任一层级名命中」判定，故 `extracted` 会保留其整棵子树。
    """
    keep = set(keep or ())
    if not keep or not target.exists():
        return set()
    return {p.relative_to(target) for p in target.rglob("*")
            if p.is_file() and any(part in keep for part in p.relative_to(target).parts)}


def verify_local_tree(target: Path, expected: dict, keep=()) -> str | None:
    """逐文件校验 Git blob SHA/size 与路径集合；通过返回 None，否则返回差异说明。"""
    if not target.is_dir() or target.is_symlink() or not isinstance(expected, dict):
        return "本地快照不是普通目录或基线非法"
    keep_rel = _keep_rel(target, keep)
    actual = {}
    for path in target.rglob("*"):
        rel = path.relative_to(target)
        if rel in keep_rel:
            continue
        if path.is_symlink():
            return f"核心路径是符号链接：{rel.as_posix()}"
        if path.is_file():
            name = rel.as_posix()
            if not _safe_relpath(name):
                return f"本地路径不安全：{name}"
            actual[name] = path
        elif not path.is_dir():
            return f"核心路径不是普通文件：{rel.as_posix()}"
    missing, extra = sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))
    if missing or extra:
        return f"路径集合不符（缺失 {missing[:5]}，多余 {extra[:5]}）"
    for name, meta in expected.items():
        try:
            data = actual[name].read_bytes()
        except OSError as exc:
            return f"读取失败 {name}：{exc}"
        if len(data) != meta["size"] or git_blob_sha(data) != meta["sha"]:
            return f"blob 不符 {name}（期望 {meta['sha']}，实得 {git_blob_sha(data)}）"
    return None


def _restore_keep(bak: Path, target: Path, keep_rel) -> None:
    for rel in keep_rel:
        srcf = bak / rel
        if srcf.is_file():
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcf, dst)


def _run_extractor(target: Path) -> None:
    """local_dir 内若有 extract_webextension.py，同步后运行它刷新 extracted/ 明文产物。"""
    exe = target / "extract_webextension.py"
    if exe.is_file():
        try:
            r = subprocess.run([sys.executable, str(exe)], cwd=str(target),
                                capture_output=True, text=True, timeout=TIMEOUT)
            tag = "已刷新 extracted/" if r.returncode == 0 else f"退出码 {r.returncode}"
            print(f"  [{target.name}] extract_webextension.py {tag}")
            if r.returncode != 0:
                print(f"    stderr: {r.stderr.strip()[:200]}")
        except Exception as e:
            print(f"  [{target.name}] extract_webextension.py 运行失败（不影响同步）：{e}")

# ---------------------------------------------------------------- web 源（官网镜像）
def web_latest(src: dict) -> dict | None:
    """拉取并交叉校验 web 源；页面、changelog 或索引任一不完整即返回 None。"""
    import hashlib
    full = _curl_text(src["llms_url"])
    if not full:
        return None
    marker_names = [m.group(1) for line in full.splitlines()
                    if (m := re.fullmatch(r"# ([A-Za-z0-9_\-]+\.md)", line))]
    if not marker_names or len(marker_names) != len(set(marker_names)):
        return None
    raw_pages = web_mirror.split_pages(full)
    if any(not any(line.strip() for line in body) for body in raw_pages.values()):
        return None
    index_url = src.get("index_url", "")
    if not index_url:
        return None
    idx = _curl_text(index_url)
    if not idx:
        return None
    mirror = web_mirror.build_mirror(full, idx, src["base_url"])
    index_names = set(web_mirror.parse_index(idx))
    index_count = sum(1 for line in idx.splitlines()
                      if re.match(r"^- \[[^\]]+\]\([A-Za-z0-9_\-]+\.md\)", line.strip()))
    if (not mirror["pages"] or "changelog.md" not in mirror["pages"]
            or not mirror["top_version"] or not index_names
            or index_count != len(index_names) or index_names != mirror["page_names"]):
        return None
    if any(not _safe_relpath(name) or "/" in name or not name.endswith(".md")
           for name in mirror["page_names"]):
        return None
    if any(not _safe_relpath(name) or "/" in name for name in mirror["images"]):
        return None
    return {
        "pages": mirror["pages"],          # OrderedDict{name: 归一后的 markdown 全文}
        "top_version": mirror["top_version"],
        "page_names": mirror["page_names"],
        "images": mirror["images"],
        "full_sha": hashlib.sha256(full.encode("utf-8")).hexdigest(),
    }


def _verify_web_stage(stage: Path, info: dict, readme_data: bytes | None) -> str | None:
    """web 候选目录必须与已校验的页面/图片集合逐字节一致，且不含链接或特殊文件。"""
    if not stage.is_dir() or stage.is_symlink():
        return "stage 不是普通目录"
    for path in stage.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            return f"stage 含非普通文件：{path.relative_to(stage)}"
    pages = info.get("pages")
    names = info.get("page_names")
    if not isinstance(pages, dict) or not pages or names != set(pages):
        return "stage 页面元数据不一致"
    actual_pages = {p.name for p in stage.glob("*.md") if p.name != "README.md"}
    if actual_pages != set(pages):
        return "stage 顶层页面集合不符"
    for name, content in pages.items():
        path = stage / name
        if not path.is_file() or path.read_bytes() != content.encode("utf-8"):
            return f"stage 页面内容不符：{name}"
    readme = stage / "README.md"
    if readme_data is None:
        if readme.exists():
            return "stage 含意外 README.md"
    elif not readme.is_file() or readme.read_bytes() != readme_data:
        return "stage README.md 不符"
    for name in info.get("images", ()):
        path = stage / "_images" / name
        if not path.is_file() or path.stat().st_size == 0:
            return f"stage 图片缺失或为空：{name}"
        try:
            data = path.read_bytes()
        except OSError as exc:
            return f"stage 图片读取失败 {name}：{exc}"
        image_format = _image_format(data)
        if image_format is None or not _image_name_matches_format(name, image_format):
            return f"stage 图片内容不是与扩展名匹配的常见格式：{name}"
    return None


def _web_fingerprint(root: Path) -> str | None:
    if not root.is_dir() or root.is_symlink():
        return None
    digest = hashlib.sha256()
    paths = sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())
    for path in paths:
        if path.is_symlink():
            return None
        if path.is_dir():
            continue
        if not path.is_file():
            return None
        rel = path.relative_to(root).as_posix()
        if rel == "README.md":
            continue
        data = path.read_bytes()
        name = rel.encode("utf-8")
        digest.update(name + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()


def web_local_matches(src: dict, info: dict) -> tuple[bool, str | None]:
    target = SKILL_ROOT / src["local_dir"]
    if not target.is_dir() or target.is_symlink():
        return False, None
    pages = info.get("pages")
    if not isinstance(pages, dict):
        return False, None
    actual_pages = {p.name for p in target.glob("*.md") if p.name != "README.md"}
    if actual_pages != set(pages):
        return False, None
    for name, content in pages.items():
        path = target / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content.encode("utf-8"):
            return False, None
    for name in info.get("images", ()):
        path = target / "_images" / name
        if path.is_symlink() or not path.is_file():
            return False, None
        data = path.read_bytes()
        image_format = _image_format(data)
        if image_format is None or not _image_name_matches_format(name, image_format):
            return False, None
    fingerprint = _web_fingerprint(target)
    if fingerprint is None:
        return False, None
    pinned = src.get("pinned_local_sha256")
    return not pinned or fingerprint == pinned, fingerprint


def sync_web_one(src: dict, force: bool = False) -> str:
    """在同卷 stage 中重建并完整校验官网镜像，成功后才替换 local_dir。"""
    info = web_latest(src)
    if info is None:
        print(f"  [{src['id']}] 上游页面或索引不完整，跳过")
        return "failed"

    pinned_ver = src.get("pinned_version", "")
    pinned_sha = src.get("pinned_full_sha", "")
    changed = force or info["top_version"] != pinned_ver or info["full_sha"] != pinned_sha
    if not changed:
        local_match, local_fingerprint = web_local_matches(src, info)
        if not local_match:
            changed = True
        elif not src.get("pinned_local_sha256") and local_fingerprint:
            try:
                _commit_source(src, {"pinned_local_sha256": local_fingerprint})
            except (OSError, ValueError) as exc:
                print(f"  [{src['id']}] 本地指纹写入失败：{exc}")
                return "failed"
            print(f"  [{src['id']}] 已补本地镜像指纹（未下载内容）")
            return "unchanged"
        else:
            print(f"  [{src['id']}] 无更新（{info['top_version']}）")
            return "unchanged"

    base_url = src["base_url"]
    target = SKILL_ROOT / src["local_dir"]
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        print(f"  [{src['id']}] local_dir 不是普通目录，放弃替换")
        return "failed"
    target.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    stage = stage_root / "snapshot"
    stage.mkdir()
    missing_imgs = []
    try:
        # 保留项先复制到 stage；任何下载或校验失败都不会改写旧镜像。
        keep_readme = target / "README.md"
        if keep_readme.is_symlink() or (keep_readme.exists() and not keep_readme.is_file()):
            return "failed"
        readme_data = keep_readme.read_bytes() if keep_readme.is_file() else None
        if readme_data is not None:
            shutil.copy2(keep_readme, stage / "README.md")
        img_dir = target / "_images"
        stage_img = stage / "_images"
        if img_dir.is_symlink() or (img_dir.exists() and not img_dir.is_dir()):
            return "failed"
        if img_dir.is_dir():
            if any(path.is_symlink() for path in img_dir.rglob("*")):
                print(f"  [{src['id']}] 旧 _images 含符号链接，放弃复制")
                return "failed"
            shutil.copytree(img_dir, stage_img, symlinks=True)
        else:
            stage_img.mkdir()

        for name in sorted(info["images"]):
            dst = stage_img / name
            if not _image_path_is_safe(dst):
                print(f"  [{src['id']}] 图片 stage 路径含符号链接：{name}")
                return "failed"
            if dst.exists() and not dst.is_file():
                print(f"  [{src['id']}] 图片 stage 路径不是普通文件：{name}")
                return "failed"
            # 非空旧图不能证明远端未变：引用到的图片每次重新下载并校验类型/签名。
            if not dst.is_file():
                missing_imgs.append(name)
            else:
                dst.unlink()
            if not _curl_bytes(f"{base_url.rstrip('/')}/_images/{name}", dst):
                print(f"  [{src['id']}] 图片下载或校验失败: {name}")
                return "failed"
        for name, content in info["pages"].items():
            (stage / name).write_text(content, encoding="utf-8", newline="\n")

        mismatch = _verify_web_stage(stage, info, readme_data)
        if mismatch is not None:
            print(f"  [{src['id']}] web stage 校验失败：{mismatch}")
            return "failed"
        local_fingerprint = _web_fingerprint(stage)
        if local_fingerprint is None:
            print(f"  [{src['id']}] web stage 指纹计算失败")
            return "failed"
        try:
            if not replace_dir(stage, target):
                print(f"  [{src['id']}] 替换失败，旧镜像未推进")
                return "failed"
        except ReplaceError as exc:
            print(f"  [{src['id']}] 回滚失败；恢复线索 {exc.backup or exc.target}")
            return "failed"
    except OSError as exc:
        print(f"  [{src['id']}] web stage 写盘失败：{exc}")
        return "failed"
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    n_pages = len(info["page_names"])
    n_imgs = sum(1 for _ in (target / "_images").iterdir())
    log_line = (f"| {date.today().isoformat()} | `{src['id']}` | web(新图 {len(missing_imgs)}) | "
                f"`{info['top_version']}` | {n_pages} 页 / {n_pages + 1 + n_imgs} 文件 |")
    try:
        _commit_source(src, {
            "pinned_version": info["top_version"],
            "pinned_full_sha": info["full_sha"],
            "pinned_pages": sorted(info["page_names"]),
            "pinned_local_sha256": local_fingerprint,
            "last_synced": date.today().isoformat(),
        }, log_line)
    except (OSError, ValueError) as exc:
        print(f"  [{src['id']}] manifest 写入失败，pin 未推进：{exc}")
        return "failed"
    print(f"  [{src['id']}] 已更新（{info['top_version']}，{n_pages} 页，新图 {len(missing_imgs)}）→ {target}")
    return "updated"


# ---------------------------------------------------------------- 增量同步（git 源）
# 全量 tarball 动辄 17~18MB，而一次上游提交常只改 1~2 个文件；本机到 GitHub 直连约
# 26KB/s，一次全量要 10 分钟以上。增量路径改走 API + raw 单文件：
#   - compare API（~1.4s）拿到变更清单（added/modified/removed/renamed）
#   - trees API（1 次请求）拿到全树 blob size，用于逐文件字节数校验
#   - raw 单文件（~1s/个）仅下载真正变动的文件
# 任一步异常（非 git 源、compare 截断、变更过多、下载或校验不符）自动回退全量 tarball，
# 绝不留下半新半旧的快照。

INCR_MAX_FILES = 120          # 变更文件超此值不建议增量，直接全量（省往返）
RAW_BASE = "https://raw.githubusercontent.com"


def _gh_api(path: str, timeout: int = 60):
    """调用 GitHub API（复用 gh CLI 凭据，带重试）。返回 dict 或 None。"""
    import time
    for attempt in range(RETRIES):
        if GH:
            try:
                r = run([GH, "api", path], timeout=timeout)
                if r.returncode == 0 and r.stdout.strip():
                    try:
                        return json.loads(r.stdout)
                    except json.JSONDecodeError:
                        pass
            except (OSError, subprocess.SubprocessError):
                pass
        if attempt < RETRIES - 1:
            time.sleep(RETRY_SLEEP)
    return None


def _safe_relpath(path: str) -> bool:
    """仅接受可安全映射到本地目录的 POSIX 相对路径（同时兼容 Windows）。"""
    return (isinstance(path, str) and bool(path) and "\\" not in path and ":" not in path
            and not any(ord(ch) < 32 for ch in path)
            and all(part not in ("", ".", "..") for part in path.split("/")))


def index_tree(repo: str, sha: str):
    """仓库全树 {path: {sha, size}}。trees 截断或结构非法时返回 None。"""
    data = _gh_api(f"repos/{repo}/git/trees/{sha}?recursive=1")
    if (not isinstance(data, dict) or type(data.get("truncated")) is not bool
            or not isinstance(data.get("tree"), list) or data["truncated"]):
        return None
    tree = {}
    for node in data["tree"]:
        if not isinstance(node, dict) or "type" not in node:
            return None
        if node["type"] != "blob":
            continue
        if not all(k in node for k in ("path", "sha", "size")):
            return None
        path, sha, size = node["path"], node["sha"], node["size"]
        if (not _safe_relpath(path) or path in tree
                or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
                or type(size) is not int or size < 0):
            return None
        tree[path] = {"sha": sha, "size": size}
    return tree


def tree_for_subpath(tree: dict, subpath: str) -> dict | None:
    """过滤仓库 tree，并把 subpath 内路径映射为相对 local_dir 的路径。"""
    if not isinstance(tree, dict) or not isinstance(subpath, str):
        return None
    if subpath == "":
        return dict(tree)
    normalized = subpath[:-1] if subpath.endswith("/") else subpath
    if not _safe_relpath(normalized):
        return None
    prefix = normalized + "/"
    result = {path[len(prefix):]: meta for path, meta in tree.items() if path.startswith(prefix)}
    return result or None


def compare_changes(repo: str, old: str, new: str):
    """compare old...new 的变更清单。清单不完整、截断或结构非法时返回 None。"""
    data = _gh_api(f"repos/{repo}/compare/{old}...{new}")
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    files = data["files"]
    if len(files) >= 300:          # GitHub compare 的文件清单上限，达到即视为不完整
        return None
    changes, seen = [], set()
    for item in files:
        if not isinstance(item, dict) or not all(k in item for k in ("filename", "status")):
            return None
        path, status = item["filename"], item["status"]
        if status == "renamed" and "previous_filename" not in item:
            return None
        previous = item["previous_filename"] if status == "renamed" else ""
        if (status not in ("added", "modified", "removed", "renamed")
                or not _safe_relpath(path) or path in seen
                or (status == "renamed" and not _safe_relpath(previous))):
            return None
        seen.add(path)
        changes.append({"path": path, "status": status, "previous_path": previous})
    return changes


def fetch_raw(repo: str, sha: str, path: str, timeout: int = 60):
    """从 raw.githubusercontent 取单文件（公开仓库免 token，保留证书校验）。"""
    import urllib.parse
    url = f"{RAW_BASE}/{repo}/{sha}/{urllib.parse.quote(path)}"
    return _http_get(url, timeout, allow_empty=True)


def local_incomplete(src: dict) -> bool:
    """本地快照是否缺失/不完整（需全量重建）；git 源逐 blob 校验，keep 项除外。

    用于 --ensure 前置检测。tree 不可达时返回 False，交给增量护栏兜底；
    tree 可访问但 subpath 非法或本地与 pinned_sha 不符时返回 True。
    """
    target = SKILL_ROOT / src["local_dir"]
    if src.get("type") == "web":
        return not (target.exists() and any(target.glob("*.md")))
    if "pinned_sha" not in src or not target.is_dir() or target.is_symlink():
        return True
    trees = index_tree(src["repo"], src["pinned_sha"])
    if trees is None:
        return False
    expected = tree_for_subpath(trees, src.get("subpath", ""))
    if expected is None:
        return True
    return verify_local_tree(target, expected, src.get("keep", ())) is not None


def sync_incremental(src, old_sha, new_sha):
    """增量更新 local_dir。返回 (是否成功, 描述, 变更文件数)。

    只落真正变化的文件；成功条件 = 每个变更文件字节数与 trees 一致。先全部下载并校验，
    再统一写盘；增量不适用或提交前失败返回 False 交给调用方回退全量。替换进入提交阶段
    后的 ReplaceError 原样上传，调用方不得再次替换可能仍由唯一备份保护的目标。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    repo, subpath = src["repo"], src.get("subpath", "")
    changes = compare_changes(repo, old_sha, new_sha)
    if changes is None:
        return False, "compare 不可用或清单截断", 0
    pre = subpath.rstrip("/") + "/" if subpath else ""
    if pre:
        changes = [c for c in changes if c["path"].startswith(pre)
                   or (c["status"] == "renamed" and c["previous_path"].startswith(pre))]
    if len(changes) > INCR_MAX_FILES:
        return False, f"变更 {len(changes)} 文件超过阈值 {INCR_MAX_FILES}", len(changes)

    new_trees = index_tree(repo, new_sha)
    if new_trees is None:
        return False, "new trees 截断", len(changes)

    # 完整性护栏：本地必须逐 blob 匹配 old_sha，不能只比较文件数。
    target = SKILL_ROOT / src["local_dir"]
    keep = src.get("keep", ())
    old_trees = index_tree(repo, old_sha)
    if old_trees is None:
        return False, f"old {old_sha[:7]} 不可比对（可能 force-push），基线未知", len(changes)
    old_core = tree_for_subpath(old_trees, subpath)
    new_core = tree_for_subpath(new_trees, subpath)
    if old_core is None or new_core is None:
        return False, "subpath 非法或不在 old/new tree 中", len(changes)
    mismatch = verify_local_tree(target, old_core, keep)
    if mismatch is not None:
        return False, f"本地快照与 {old_sha[:7]} 不符：{mismatch}", len(changes)
    if not changes:
        if old_core != new_core:
            return False, "compare 未列出 subpath 内的 tree 变化", 0
        return True, "无文件变化", 0

    downloads = []      # [(新路径, 期望字节数)]
    deletes = []        # 待删路径
    for c in changes:
        st, path = c["status"], c["path"]
        if st == "renamed" and c["previous_path"]:
            old_p = c["previous_path"]
            if not pre or old_p.startswith(pre):
                deletes.append(old_p)
        if st == "removed":
            deletes.append(path)
        elif not pre or path.startswith(pre):
            rel = path[len(pre):]
            if rel not in new_core:
                return False, f"new trees 缺少 {path}", len(changes)
            downloads.append((path, new_core[rel]))


    blobs = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_raw, repo, new_sha, p): (p, sz) for p, sz in downloads}
        for fut in as_completed(futs):
            path, want = futs[fut]
            try:
                data = fut.result()
            except Exception as exc:
                return False, f"下载异常 {path}：{exc}", len(changes)
            if data is None:
                return False, f"下载失败 {path}", len(changes)
            if len(data) != want["size"]:
                return False, f"字节数不符 {path}（期望 {want['size']} 实得 {len(data)}）", len(changes)
            if git_blob_sha(data) != want["sha"]:
                return False, f"blob 不符 {path}（期望 {want['sha']} 实得 {git_blob_sha(data)}）", len(changes)
            blobs[path] = data

    stage_root = None
    try:
        stage_root = Path(tempfile.mkdtemp(prefix="xlwings_inc_"))
        stage = stage_root / "snapshot"
        shutil.copytree(target, stage, symlinks=True)
        # keep 文件不进入上游 stage，替换时由 replace_dir 从旧目标恢复。
        for rel in _keep_rel(stage, keep):
            (stage / rel).unlink()

        def _local(p):
            return stage / (p[len(pre):] if pre else p)

        for p in deletes:
            fp = _local(p)
            if fp.is_symlink() or fp.is_file():
                fp.unlink()
            elif fp.is_dir():
                shutil.rmtree(fp)
        for p, data in blobs.items():
            fp = _local(p)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(data)
        mismatch = verify_local_tree(stage, new_core, keep)
        if mismatch is not None:
            return False, f"stage 与 new tree 不符：{mismatch}", len(changes)
        if not replace_dir(stage, target, keep):
            return False, "stage 替换失败", len(changes)
    except OSError as exc:
        return False, f"stage 写盘失败：{exc}", len(changes)
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)
    return True, f"增量 {len(blobs)} 改 / {len(deletes)} 删", len(changes)

@contextmanager
def _replacement_lock(target: Path):
    """串行化同一目标的替换：进程内 RLock + 跨进程 OS 文件锁（崩溃自动释放）。"""
    import time
    with _REPLACE_THREAD_LOCK:
        lock_root = Path(tempfile.gettempdir()) / "xlwings-sync-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(os.fsencode(os.path.normcase(os.path.abspath(target)))).hexdigest()
        lock_path = lock_root / f"{key}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            deadline = time.monotonic() + TIMEOUT
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            yield
        finally:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(fd)


class ReplaceError(RuntimeError):
    """替换已进入提交阶段但回滚失败；backup/target 保留可供人工恢复的线索。"""

    def __init__(self, message: str, backup: Path | None, target: Path):
        super().__init__(message)
        self.backup = backup
        self.target = target


def _rollback_replace(target: Path, bak: Path | None) -> bool:
    """删除新半成品并恢复备份；任何失败都保留 bak，绝不删除旧快照。"""
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        if bak is not None and bak.exists():
            bak.rename(target)
    except OSError:
        return False
    return True


def _recover_interrupted_replace(target: Path) -> bool:
    """持锁恢复上次在“目标改名后、候选提交前”中断的唯一备份；歧义时拒绝猜测。"""
    if target.exists() or target.is_symlink():
        return True
    backups = sorted(target.parent.glob(f".{target.name}.old-*"))
    if not backups:
        return True
    if len(backups) != 1 or not backups[0].is_dir() or backups[0].is_symlink():
        print(f"  替换恢复歧义，保留全部线索：{', '.join(str(p) for p in backups)}")
        return False
    try:
        backups[0].rename(target)
        for stale in target.parent.glob(f".{target.name}.new-*"):
            shutil.rmtree(stale)
    except OSError as exc:
        print(f"  中断替换恢复失败，保留备份 {backups[0]}: {exc}")
        return False
    return True


def _replace_dir_unlocked(stage: Path, target: Path, keep=()) -> bool:
    """同卷候选目录 + 唯一备份：先完整复制/校验，再 rename 备份并 os.replace 提交。

    keep 项在旧目标仍存在时复制到候选目录。普通失败自动回滚；回滚失败抛
    ReplaceError，并保留唯一备份路径。进程在两次重命名之间退出时，下一次持锁替换先恢复备份。
    """
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return False
    if not _recover_interrupted_replace(target):
        return False
    if not stage.is_dir() or stage.is_symlink():
        return False
    candidate = None
    bak = None
    try:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            keep_rel = _keep_rel(target, keep)
            candidate = Path(tempfile.mkdtemp(prefix=f".{target.name}.new-", dir=target.parent))
            shutil.copytree(stage, candidate, dirs_exist_ok=True, symlinks=True)
            if keep_rel:
                _restore_keep(target, candidate, keep_rel)
            if count_files(candidate) != count_files(stage) + len(keep_rel):
                return False
        except OSError:
            return False

        if target.exists():
            bak = target.parent / f".{target.name}.old-{uuid.uuid4().hex}"
            try:
                target.rename(bak)
            except OSError:
                return False
        try:
            os.replace(candidate, target)
        except OSError:
            if not _rollback_replace(target, bak):
                clue = bak if bak is not None else target
                raise ReplaceError(f"回滚失败；恢复线索：{clue}（目标 {target}）",
                                   backup=bak, target=target)
            return False
        if bak is not None:
            try:
                shutil.rmtree(bak)
            except OSError as exc:
                print(f"  替换成功但备份清理失败，保留恢复线索 {bak}: {exc}")
        return True
    finally:
        if candidate is not None:
            # 提交成功后 candidate 已改名；失败时清理半成品，旧目标或唯一备份仍保留。
            shutil.rmtree(candidate, ignore_errors=True)


def replace_dir(stage: Path, target: Path, keep=()) -> bool:
    """获取跨进程锁后执行替换；锁不可用时安全失败，ReplaceError 仍明确上报。"""
    try:
        with _replacement_lock(target):
            return _replace_dir_unlocked(stage, target, keep)
    except OSError:
        return False


def sync_one(src: dict, force: bool = False) -> str:
    """同步单路上游，返回 updated / unchanged / failed。"""
    vid, repo, ref = src["id"], src["repo"], src["ref"]
    head = latest_sha(repo, ref, src.get("ref_kind"))
    if head is None:
        print(f"  [{vid}] 无法获取远端 commit，跳过")
        return "failed"
    if not force and head == src.get("pinned_sha"):
        print(f"  [{vid}] 无更新（{head[:7]}）")
        return "unchanged"
    print(f"  [{vid}] 检测到更新 {src.get('pinned_sha', '?')[:7]} → {head[:7]}")

    # local_dir 是固定目录名，不随上游版本变化：上游出新版只改 manifest 的 ref，
    # 自研文档里引用的路径（如 xlwings/xlwings/cli.py）因此长期有效。
    target = SKILL_ROOT / src["local_dir"]
    old_sha = src.get("pinned_sha", "")
    keep = src.get("keep", ())
    channel, n_changed = "incremental", 0

    # 优先增量：只下载 compare 出的变更文件，避免为 1~2 个文件重拉 17MB 全量包。
    # force / 首次同步（无旧 sha 或本地无快照）时无从比对，直接走全量。
    inc_ok = False
    if not force and old_sha and target.exists():
        try:
            inc_ok, desc, n_changed = sync_incremental(src, old_sha, head)
        except ReplaceError as exc:
            print(f"  [{vid}] 增量替换回滚失败；禁止再次替换，恢复线索 {exc.backup or exc.target}")
            return "failed"
        print(f"  [{vid}] {'增量成功：' if inc_ok else '增量不适用（回退全量）：'}{desc}")

    if not inc_ok:
        head_tree = index_tree(repo, head)
        expected_files = len(head_tree) if head_tree is not None else None
        stage, channel = download_tarball(repo, head, expected_files=expected_files,
                                          tree_verified=head_tree is not None)
        if stage is None:
            print(f"  [{vid}] 全量下载/解压失败（允许通道均不可达）")
            return "failed"
        subpath = src.get("subpath", "")
        if subpath:
            normalized = subpath[:-1] if subpath.endswith("/") else subpath
            if not _safe_relpath(normalized):
                print(f"  [{vid}] subpath 非法，放弃替换")
                return "failed"
            stage = stage / normalized
            if not stage.is_dir() or stage.is_symlink():
                print(f"  [{vid}] tarball 缺少 subpath {subpath}，放弃替换")
                return "failed"
        if head_tree is not None:
            expected = tree_for_subpath(head_tree, subpath)
            if expected is None:
                print(f"  [{vid}] subpath 不在 HEAD tree 中，放弃替换")
                return "failed"
            mismatch = verify_local_tree(stage, expected)
            if mismatch is not None:
                print(f"  [{vid}] 全量 stage 与 HEAD tree 不符：{mismatch}")
                return "failed"
        if count_files(stage) == 0:
            print(f"  [{vid}] 全量下载结果为空，放弃替换")
            return "failed"
        try:
            if not replace_dir(stage, target, keep):
                print(f"  [{vid}] 替换失败（旧快照保留或已回滚）")
                return "failed"
        except ReplaceError as exc:
            print(f"  [{vid}] 回滚失败；恢复线索 {exc.backup or exc.target}")
            return "failed"

    if target.exists() and (target / "extract_webextension.py").is_file():
        _run_extractor(target)
    n_files = count_files(target)
    chan = f"{channel}（{n_changed} 处变更）" if channel == "incremental" else channel
    log_line = f"| {date.today().isoformat()} | `{vid}` | {chan} | `{head[:7]}` | {n_files} 个文件 |"
    try:
        _commit_source(src, {"pinned_sha": head, "last_synced": date.today().isoformat()}, log_line)
    except (OSError, ValueError) as exc:
        print(f"  [{vid}] manifest 写入失败，目录已更新但 pin 未推进：{exc}")
        return "failed"
    print(f"  [{vid}] 已更新（{channel}，{n_files} 文件）→ {target}")
    return "updated"


def _check_source(src: dict) -> tuple[str, str]:
    try:
        if src.get("type") == "web":
            info = web_latest(src)
            pinned_ver = src.get("pinned_version", "")
            if info is None:
                return "远端不可达", "failed"
            if info["top_version"] != pinned_ver or info["full_sha"] != src.get("pinned_full_sha", ""):
                return f"有更新（{pinned_ver} → {info['top_version']}）", "updated"
            local_match, _ = web_local_matches(src, info)
            if not local_match:
                return "本地镜像不完整", "updated"
            return f"已是最新（{pinned_ver}）", "unchanged"
        head = latest_sha(src["repo"], src["ref"], src.get("ref_kind"))
        if head is None:
            return "远端不可达", "failed"
        return ("有更新", "updated") if head != src.get("pinned_sha") else ("已是最新", "unchanged")
    except Exception:
        return "检测异常", "failed"


def main() -> int:
    args = sys.argv[1:]
    check_only = "--check" in args
    ensure = "--ensure" in args
    force_ids = [a for a in args[args.index("--force") + 1:] if not a.startswith("--")] \
        if "--force" in args else []
    global MANIFEST_CFG
    MANIFEST_CFG = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    stats: dict[str, list[str]] = {"updated": [], "unchanged": [], "failed": []}
    if ensure:
        need = [s_["id"] for s_ in MANIFEST_CFG["sources"] if local_incomplete(s_)]
        force_ids = list(dict.fromkeys(force_ids + need))
        print(f"[ensure] 本地缺失/不完整、需全量重建的源：{need or '无（本地均已就绪）'}")
        print(f"[ensure] 其余源将按增量追新或跳过；完整且最新则不动。")
    sources = [src for src in MANIFEST_CFG["sources"]
               if not force_ids or ensure or src["id"] in force_ids]
    if check_only:
        from concurrent.futures import ThreadPoolExecutor
        if sources:
            with ThreadPoolExecutor(max_workers=min(3, len(sources))) as pool:
                results = list(pool.map(_check_source, sources))
        else:
            results = []
        for src, (state, res) in zip(sources, results):
            suffix = ", web" if src.get("type") == "web" else ""
            print(f"[check] {src['id']} ({src.get('local_dir')}{suffix}): {state}")
            stats[res].append(src["id"])
        return 1 if stats["failed"] else 0

    for src in sources:
        if src.get("type") == "web":
            res = sync_web_one(src, force=(src["id"] in force_ids))
            stats[res].append(src["id"])
            continue
        if src["id"] in force_ids:
            result = sync_one(src, force=True)
        else:
            head = latest_sha(src["repo"], src["ref"], src.get("ref_kind"))
            if head and head != src.get("pinned_sha"):
                result = sync_one(src)
            elif head:
                result = "unchanged"
            else:
                result = "failed"
        stats[result].append(src["id"])
    if not check_only:
        print("\n完成：" + "; ".join(f"{k}: {'、'.join(v) if v else '无'}" for k, v in stats.items()))
    return 1 if stats["failed"] else 0


MANIFEST_CFG = None
if __name__ == "__main__":
    sys.exit(main())
