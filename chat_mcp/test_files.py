import base64
import os

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
