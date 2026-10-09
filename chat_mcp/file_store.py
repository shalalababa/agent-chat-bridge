"""Linux file access confined to one configured root, using directory FDs."""
import base64
import hashlib
import os
from pathlib import Path
import stat
import threading
import uuid
from contextlib import contextmanager


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FileStore:
    def __init__(self, root, max_bytes=8 * 1024 * 1024):
        self.root = Path(root).resolve(strict=True)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.mkdir('.history', mode=0o700, dir_fd=self.root_fd)
        except FileExistsError:
            pass
        self.history_fd = os.open('.history', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.root_fd)

    def close(self):
        os.close(self.history_fd)
        os.close(self.root_fd)

    def _parts(self, path):
        parts = path.split('/')
        if not path or any(p in ('', '.', '..', '.history', '.git') for p in parts):
            raise ValueError('Use a relative path without empty, dot, parent, .git or .history components')
        if '\x00' in path:
            raise ValueError('Invalid path')
        return parts

    @contextmanager
    def _parent(self, path):
        parts = self._parts(path)
        fd = os.dup(self.root_fd)
        try:
            for part in parts[:-1]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def _read(self, fd, name):
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, 'rb') as f:
            meta = os.fstat(f.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                raise ValueError('Only regular files with one hard link are supported')
            data = f.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise ValueError('File exceeds configured byte limit')
            return data, stat.S_IMODE(meta.st_mode)

    def read_file(self, path, encoding='utf-8'):
        with self.lock, self._parent(path) as (fd, name):
            data, _ = self._read(fd, name)
        if encoding not in ('utf-8', 'base64'):
            raise ValueError('encoding must be utf-8 or base64')
        content = data.decode('utf-8') if encoding == 'utf-8' else base64.b64encode(data).decode('ascii')
        return dict(path=path, content=content, encoding=encoding, bytes=len(data), sha256=digest(data))

    def write_file(self, path, content, encoding='utf-8', expected_sha256=None):
        if len(content) > self.max_bytes * 2:
            raise ValueError('Content exceeds configured byte limit')
        if encoding == 'utf-8':
            data = content.encode('utf-8')
        elif encoding == 'base64':
            data = base64.b64decode(content, validate=True)
        else:
            raise ValueError('encoding must be utf-8 or base64')
        if len(data) > self.max_bytes:
            raise ValueError('Content exceeds configured byte limit')
        with self.lock, self._parent(path) as (fd, name):
            try:
                old, mode = self._read(fd, name)
            except FileNotFoundError:
                old, mode = None, 0o600
            if old is not None and expected_sha256 != digest(old):
                raise ValueError('Existing file requires its current sha256 from read_file')
            if old is None and expected_sha256 is not None:
                raise ValueError('Expected existing file is missing')
            backup = None
            if old is not None:
                backup = uuid.uuid4().hex
                bfd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.history_fd)
                with os.fdopen(bfd, 'wb') as f:
                    f.write(old)
                    f.flush()
                    os.fsync(f.fileno())
            temporary = '.mcp-' + uuid.uuid4().hex
            tfd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            try:
                with os.fdopen(tfd, 'wb') as f:
                    f.write(data)
                    f.flush()
                    os.fchmod(f.fileno(), mode & 0o777)
                    os.fsync(f.fileno())
                if old is None:
                    # link is an atomic no-clobber publish if another writer created the name.
                    os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                else:
                    current, _ = self._read(fd, name)
                    if digest(current) != expected_sha256:
                        raise ValueError('File changed during write; retry after reading')
                    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
            return dict(path=path, bytes=len(data), sha256=digest(data), backup=backup)

    def edit_file(self, path, old_text, new_text, expected_sha256):
        with self.lock:
            data = self.read_file(path)
            if not old_text or data['content'].count(old_text) != 1:
                raise ValueError('old_text must match exactly once')
            return self.write_file(path, data['content'].replace(old_text, new_text, 1), expected_sha256=expected_sha256)

    def make_directory(self, path):
        with self.lock, self._parent(path) as (fd, name):
            os.mkdir(name, mode=0o700, dir_fd=fd)
        return dict(path=path, created=True)

    def list_files(self, path=''):
        with self.lock:
            if path:
                with self._parent(path) as (parent, name):
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            else:
                fd = os.dup(self.root_fd)
            try:
                entries = []
                with os.scandir(fd) as scan:
                    for entry in scan:
                        if entry.name in ('.history', '.git') or entry.name.startswith('.mcp-'):
                            continue
                        if len(entries) >= 1000:
                            return dict(path=path, entries=entries, truncated=True)
                        entries.append(dict(name=entry.name, directory=entry.is_dir(follow_symlinks=False), symlink=entry.is_symlink()))
                return dict(path=path, entries=sorted(entries, key=lambda e: e['name']), truncated=False)
            finally:
                os.close(fd)
