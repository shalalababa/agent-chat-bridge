import base64
from concurrent.futures import ThreadPoolExecutor
import os
from unittest.mock import patch

import pytest

from file_store import FileStore


@pytest.fixture
def store(tmp_path):
    value = FileStore(tmp_path, max_bytes=1024)
    yield value
    value.close()


def test_create_read_edit_and_backup(store):
    first = store.write_file('note.txt', 'hello')
    assert store.read_file('note.txt')['content'] == 'hello'
    second = store.edit_file('note.txt', 'hello', 'world', first['sha256'])
    assert store.read_file('note.txt')['content'] == 'world'
    assert second['sha256'] != first['sha256']
    assert (store.root / '.history' / second['backup']).read_bytes() == b'hello'


def test_binary_upload(store):
    raw = bytes(range(256))
    store.write_file('data.bin', base64.b64encode(raw).decode(), encoding='base64')
    assert base64.b64decode(store.read_file('data.bin', 'base64')['content']) == raw


def test_stale_or_missing_hash_cannot_overwrite(store):
    store.write_file('note', 'original')
    for digest in [None, 'wrong']:
        with pytest.raises(ValueError):
            store.write_file('note', 'lost', expected_sha256=digest)
    assert store.read_file('note')['content'] == 'original'


@pytest.mark.parametrize('path', ['../escape', '/tmp/escape', '.history/hidden', 'a/../escape', '.git/config'])
def test_paths_are_confined(store, path):
    with pytest.raises(ValueError):
        store.write_file(path, 'bad')


def test_symlinks_and_hardlinks_rejected(store, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.write_text('outside')
    (tmp_path / 'link').symlink_to(outside)
    os.link(outside, tmp_path / 'hardlink')
    for path in ['link', 'hardlink']:
        with pytest.raises((ValueError, OSError)):
            store.read_file(path)
        with pytest.raises((ValueError, OSError)):
            store.write_file(path, 'bad')
    (tmp_path / 'dirlink').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        store.write_file('dirlink/escape', 'bad')


def test_limit_and_ambiguous_edit(store):
    with pytest.raises(ValueError):
        store.write_file('large', 'x' * 1025)
    info = store.write_file('note', 'same same')
    with pytest.raises(ValueError):
        store.edit_file('note', 'same', 'new', info['sha256'])
    assert store.read_file('note')['content'] == 'same same'


def test_directory_listing(store):
    store.make_directory('sub')
    store.write_file('sub/note', 'ok')
    assert store.list_files('sub')['entries'][0]['name'] == 'note'
    assert '.history' not in [x['name'] for x in store.list_files()['entries']]


def test_rejected_path_variants_and_oversized_edits(store, tmp_path):
    for path in ('/etc/passwd', 'sub/../../outside', '.history', 'sub/.history/file',
                 'sub/.git/file', 'sub//file', 'sub/./file', 'sub/\x00file', 'x' * 4096):
        with pytest.raises((ValueError, OSError)):
            store.write_file(path, 'bad')
    first = store.write_file('note', 'short')
    with pytest.raises(ValueError):
        store.edit_file('note', 'short', 'x' * 1025, first['sha256'])
    assert store.read_file('note')['content'] == 'short'
    assert not list((tmp_path / '.history').iterdir())


def test_binary_decode_and_unicode_names(store, tmp_path):
    for encoded in ('%%%=', 'YWJj=', 'YWJj\n'):
        with pytest.raises(ValueError):
            store.write_file('invalid', encoded, encoding='base64')
    store.write_file('．history', 'unicode name')
    assert store.read_file('．history')['content'] == 'unicode name'
    store.write_file('.HISTORY', 'different case')
    assert store.read_file('.HISTORY')['content'] == 'different case'
    assert not (tmp_path / 'invalid').exists()


def test_concurrent_sha_guard_allows_only_one_replacement(store):
    first = store.write_file('note', 'old')
    def replace(value):
        try:
            return store.write_file('note', value, expected_sha256=first['sha256'])
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, ('first', 'second')))
    assert sum(result is not None for result in results) == 1
    assert store.read_file('note')['content'] in ('first', 'second')


def test_leaf_symlink_swap_does_not_follow_target(store, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-target')
    outside.write_text('outside')
    original = store.write_file('note', 'old')
    original_read = store._read
    reads = 0

    def swap_after_check(fd, name):
        nonlocal reads
        result = original_read(fd, name)
        reads += 1
        if reads == 2:
            (tmp_path / 'note').unlink()
            (tmp_path / 'note').symlink_to(outside)
        return result

    with patch.object(store, '_read', side_effect=swap_after_check):
        store.write_file('note', 'new', expected_sha256=original['sha256'])
    assert outside.read_text() == 'outside'
    assert not (tmp_path / 'note').is_symlink()


def test_parent_symlink_swap_stays_on_open_directory(store, tmp_path):
    store.make_directory('sub')
    outside = tmp_path.parent / (tmp_path.name + '-outside-dir')
    outside.mkdir()
    original_open = os.open

    def swap_parent(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == 'sub':
            (tmp_path / 'sub').rename(tmp_path / 'original-sub')
            (tmp_path / 'sub').symlink_to(outside, target_is_directory=True)
        return fd

    with patch('file_store.os.open', side_effect=swap_parent):
        store.write_file('sub/file', 'inside')
    assert (tmp_path / 'original-sub/file').read_text() == 'inside'
    assert not (outside / 'file').exists()


@pytest.mark.xfail(strict=True, reason='A local process can move an open parent directory outside the root')
def test_concurrent_parent_move_cannot_write_outside(store, tmp_path):
    store.make_directory('sub')
    outside = tmp_path.parent / (tmp_path.name + '-moved-parent')
    outside.mkdir()
    original_open = os.open

    def move_parent_after_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == 'sub':
            (tmp_path / 'sub').rename(outside / 'sub')
        return fd

    with patch('file_store.os.open', side_effect=move_parent_after_open):
        store.write_file('sub/escaped', 'outside')
    assert not (outside / 'sub/escaped').exists()
