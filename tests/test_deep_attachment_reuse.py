import pytest
from app import deep_research, documents, public_pdf


def test_same_large_pdf_reads_attached_pages_instead_of_downloading(monkeypatch):
    monkeypatch.setattr(documents, 'attached_documents', lambda c: [
        {'id': 'bank', 'source_url': 'https://static.cninfo.com.cn/annual.PDF', 'page_count': 350}])
    monkeypatch.setattr(public_pdf, 'read', lambda *a, **k: pytest.fail('already attached PDF must not redownload'))
    searches, reads = [], []
    def search(chat, queries, did):
        searches.append((queries, did))
        return {'snippets': [{'page': 14, 'text': 'short search excerpt'}, {'page': 14}, {'page': 201}]}
    def read(chat, did, page, count):
        reads.append((did, page, count))
        return {'snippets': [{'document_id': did, 'page': page, 'text': 'actually read full page'}]}
    monkeypatch.setattr(documents, 'search_attached', search)
    monkeypatch.setattr(documents, 'read_attached', read)
    result = deep_research.read_research_source({}, 'http://static.cninfo.com.cn/annual.PDF#page=14', '2026-10-16', ['营业收入'])
    assert searches == [(['营业收入'], 'bank')]
    assert reads == [('bank', 14, 1), ('bank', 201, 1)]
    assert result['reused_attached_document'] == 'bank'
    assert all(s['text'] == 'actually read full page' for s in result['snippets'])


def test_unattached_pdf_keeps_public_transport_and_cutoff(monkeypatch):
    monkeypatch.setattr(documents, 'attached_documents', lambda c: [])
    calls = []
    monkeypatch.setattr(public_pdf, 'read', lambda url, **kwargs: calls.append((url, kwargs)) or {'status': 'ok'})
    deep_research.read_research_source({}, 'https://example.test/report.pdf', '2026-10-16', ['收入'])
    assert calls == [('https://example.test/report.pdf', {'as_of': '2026-10-16', 'queries': ['收入']})]


def test_search_excerpt_does_not_claim_local_body_when_no_page_found(monkeypatch):
    monkeypatch.setattr(documents, 'attached_documents', lambda c: [{'id': 'a', 'source_url': 'https://example.test/a.pdf'}])
    monkeypatch.setattr(documents, 'search_attached', lambda *a: {'snippets': []})
    monkeypatch.setattr(documents, 'read_attached', lambda *a: pytest.fail('no known target page'))
    result = deep_research.read_research_source({}, 'https://example.test/a.pdf', '2026-10-16', ['未知关键词'])
    assert result['status'] == 'empty' and result['snippets'] == []
