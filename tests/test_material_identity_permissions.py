"""URL/document aliases cannot evade the same human material restriction."""
from copy import deepcopy

import pytest

from app import agent, documents, store, turn_constraints


BLOCKED = 'abcdef0123456789'
ALLOWED = '1234567890abcdef'
URL = 'https://example.com/annual.pdf'


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    store.put_document({'id': BLOCKED, 'source_url': URL, 'title': '受限年报'})
    store.put_document({'id': ALLOWED, 'source_url': 'https://example.com/other.pdf', 'title': '其他年报'})
    chat = store.new_conversation()
    store.update_conversation(chat['id'], lambda c: c.update(document_ids=[BLOCKED, ALLOWED]))
    monkeypatch.setattr(documents, 'ensure_index', lambda *a: pytest.fail('permission checks must not read a PDF'))
    return chat


def turn(chat, text):
    return store.begin_turn(chat['id'], text, 'stub', 'documents')


@pytest.mark.parametrize('tool,arguments', [
    ('read_document', {'document_id': BLOCKED, 'page': 7}),
    ('read_document', {'document_id': BLOCKED, 'page': 7, 'url': 'https://example.com/safe.pdf'}),
    ('search_documents', {'document_id': BLOCKED, 'queries': ['营业收入']}),
    ('search_documents', {'queries': ['营业收入']}),
    ('read_public_pdf', {'url': URL}),
    ('read_webpage', {'url': URL}),
    ('build_investment_memo', {'url': 'https://example.com/safe.pdf', 'document_id': ALLOWED}),
])
def test_url_ban_blocks_local_alias_and_full_library_search(catalog, tool, arguments):
    chat = turn(catalog, '不要读取PDF ' + URL + '。')
    original = deepcopy(arguments)
    with pytest.raises(ValueError, match='禁止读取该材料'):
        agent.tool(chat, tool, arguments)
    assert arguments == original


def test_url_ban_still_allows_other_explicit_document(catalog):
    chat = turn(catalog, '不要读取PDF ' + URL + '。')
    turn_constraints.enforce(chat, 'read_document', {'document_id': ALLOWED, 'page': 7})
    turn_constraints.enforce(chat, 'search_documents', {'document_id': ALLOWED, 'queries': ['营业收入']})


def test_full_library_search_without_blocked_document_is_allowed(catalog):
    store.update_conversation(catalog['id'], lambda c: c.update(document_ids=[ALLOWED]))
    chat = turn(catalog, '不要读取PDF ' + URL + '。')
    turn_constraints.enforce(chat, 'search_documents', {'queries': ['营业收入']})


def test_run_attached_pdf_is_checked_even_when_chat_has_no_document_ids(catalog):
    run = store.create_run({'ticker': '300750.SZ', 'year': 2025, 'as_of': '2026-10-16', 'mode': 'sample'})
    store.update_run(run['id'], lambda r: r.update(state='completed', document_ids=[BLOCKED]))
    store.update_conversation(catalog['id'], lambda c: c.update(run_id=run['id'], document_ids=[]))
    chat = turn(catalog, '不要读取PDF ' + URL + '。')
    with pytest.raises(ValueError, match='禁止读取该材料'):
        turn_constraints.enforce(chat, 'search_documents', {'queries': ['营业收入']})


def test_pdf_url_page_ban_retains_page_scope_for_local_alias(catalog):
    chat = turn(catalog, '不要读取PDF ' + URL + ' 第7页。')
    with pytest.raises(ValueError, match='禁止读取该材料'):
        turn_constraints.enforce(chat, 'read_document', {'document_id': BLOCKED, 'page': 7})
    turn_constraints.enforce(chat, 'read_document', {'document_id': BLOCKED, 'page': 8})
    with pytest.raises(ValueError, match='禁止读取该材料'):
        turn_constraints.enforce(chat, 'search_documents', {'document_id': BLOCKED, 'queries': ['营业收入']})
    # Search and public-PDF reading do not implement a page selector. An extra
    # ignored page=8 must not authorize them to access forbidden page 7.
    for name, args in [('search_documents', {'document_id': BLOCKED, 'page': 8}),
                       ('read_public_pdf', {'url': URL, 'page': 8})]:
        with pytest.raises(ValueError, match='禁止读取该材料'):
            turn_constraints.enforce(chat, name, args)


def test_document_id_ban_cannot_be_bypassed_by_public_pdf_url(catalog):
    chat = turn(catalog, '不要读取文档' + BLOCKED + '。')
    with pytest.raises(ValueError, match='禁止读取该材料'):
        turn_constraints.enforce(chat, 'read_public_pdf', {'url': URL, 'document_id': ALLOWED})
    turn_constraints.enforce(chat, 'read_public_pdf', {'url': 'https://example.com/other.pdf'})


def test_canonical_public_url_matches_saved_pdf_source(catalog):
    chat = turn(catalog, '不要读取PDF http://EXAMPLE.COM/annual.pdf?utm_source=test。')
    with pytest.raises(ValueError, match='禁止读取该材料'):
        turn_constraints.enforce(chat, 'read_document', {'document_id': BLOCKED, 'page': 7})


def test_page_only_ban_cannot_use_ignored_search_selector(catalog):
    chat = turn(catalog, '不要读取PDF第7页。')
    with pytest.raises(ValueError, match='禁止读取该材料'):
        turn_constraints.enforce(chat, 'search_documents', {'page': 8, 'queries': ['营业收入']})
