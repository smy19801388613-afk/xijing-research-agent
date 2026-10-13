import copy
import pytest
from app.evidence_policy import eligibility, source_fingerprint
from app.deep_research import citable_source
from app import questions


def source(**changes):
    return {'id':'record','kind':'web','read_status':'body_read','text':'公司已经签订付费合同。',
            'published_at':'2026-08-01',**changes}


@pytest.mark.parametrize('changes,basis,body,supported,inference',[
    ({},'body',True,True,True),
    ({'read_status':'search_snippet'},'lead',False,False,False),
    ({'read_status':'pdf_link_only'},'lead',False,False,False),
    ({'read_status':'failed'},'missing',False,False,False),
    ({'read_status':'body_read','text':''},'missing',False,False,False),
    ({'read_status':'windows_ocr'},'ocr',True,False,True),
    ({'published_at':None},'body',True,False,True),
    ({'published_at':'2026-02-30'},'body',True,False,True),
    ({'published_at':'2026-10-01'},'body',True,False,False),
])
def test_shared_eligibility_and_report_claims(changes,basis,body,supported,inference):
    record=source(**changes);before=copy.deepcopy(record)
    run={'request':{'as_of':'2026-09-28'}}
    policy=eligibility(record,run)
    assert (policy['basis'],policy['readable_body'],policy['supports_claim'],policy['supports_inference'])==(basis,body,supported,inference)
    assert citable_source(record,'supported',run)==supported
    assert citable_source(record,'inference',run)==inference
    assert record==before


def test_historical_age_does_not_expire_evidence_and_compact_dates_work():
    assert eligibility(source(published_at='20100331'),as_of='2026-09-28')['supports_answer']
    assert not eligibility(source(published_at='20260929'),as_of='2026-09-28')['can_cite']


def test_conflicting_fact_disqualifies_related_body():
    run={'request':{'as_of':'2026-09-28'},'facts':[{'id':'revenue-2025','value':'100','status':'conflict','evidence_ids':['record']} ]}
    policy=eligibility(source(),run)
    assert policy['basis']=='conflict' and not policy['supports_answer']
    assert not citable_source(source(),'supported',run)


def test_retrieval_time_does_not_change_source_identity_but_date_or_text_does():
    old=source(retrieved_at='2026-10-01')
    assert source_fingerprint(old)==source_fingerprint({**old,'retrieved_at':'2026-10-02'})
    assert source_fingerprint(old)!=source_fingerprint({**old,'text':old['text']+'补充说明。'})
    assert source_fingerprint(old)!=source_fingerprint({**old,'published_at':None})


def test_calculation_is_not_original_question_evidence():
    policy=eligibility({'id':'calc','kind':'calculation','value':'20'})
    assert policy['supports_claim'] and not policy['supports_answer']
