import copy
import pytest
from fastapi.testclient import TestClient
from app import store, questions, main, deep_research, agent

@pytest.fixture
def run(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path)
    r=store.create_run({'ticker':'600001.SH','year':2025,'as_of':'2026-09-28','mode':'live','question':'有没有付费合同？'})
    r.update(state='completed_with_gaps',research_sources=[{'id':'official','kind':'web','title':'公司披露','text':'公司已签订付费合同，合同金额为人民币一百万元。','published_at':'2026-08-01','read_status':'body_read','url':'https://example.com/contract'}]);store.save_run(r)
    return r


def answer(run,**fields):
    return {'question_id':questions.inventory(run)[0]['id'],'answer':'披露称已签订付费合同，尚未证明回款。','status':'supported',
        'bindings':[{'source_id':'official','quote':'公司已签订付费合同','relation':'support','reason':'该原文明确描述合同为付费合同。'}],
        'counter_evidence':'合同尚不等于实际回款。','gaps':[],'next_action':'','assessment_origin':'model',**fields}


def test_new_and_old_research_have_pending_questions(run):
    assert questions.snapshot(run)['counts']['pending']==1
    old=copy.deepcopy(run);old.pop('research_questions')
    assert questions.snapshot(old)['counts']['pending']==1 and 'research_questions' not in old


def test_excerpt_supported_with_explicit_semantic_boundary(run):
    v=questions.update(run['id'],[answer(run)])
    assert v['complete'] and v['counts']['supported']==1
    assert v['items'][0]['assessment_origin']=='model'
    assert 'requires_review' in v['items'][0]['semantic_validation']

@pytest.mark.parametrize('fields',[{'bindings':[]},{'answer':''},{'gaps':['未取得合同原件']},{'counter_evidence':''}])
def test_supported_requires_answer_and_no_missing_evidence(run,fields):
    v=questions.update(run['id'],[answer(run,**fields)])
    assert not v['complete'] and v['counts']['partial']==1

@pytest.mark.parametrize('changed',[{'read_status':'search_snippet'},{'published_at':None},{'read_status':'windows_ocr'}])
def test_source_count_or_ocr_not_completion(run,changed):
    run['research_sources'][0].update(changed);store.save_run(run)
    v=questions.update(run['id'],[answer(run)])
    assert v['counts']['partial']==1 and v['items'][0]['gaps']

@pytest.mark.parametrize('binding',[{'source_id':'invented','quote':'公司已签订付费合同','reason':'支持'},
    {'source_id':'official','quote':'公司已收到回款一百万元','reason':'支持'}])
def test_fake_source_or_quote_rejected_atomically(run,binding):
    before=store.get_run(run['id'])
    with pytest.raises(ValueError):questions.update(run['id'],[answer(run,bindings=[binding])])
    assert store.get_run(run['id'])==before


def test_future_source_cannot_be_used(run):
    run['research_sources'][0]['published_at']='2026-10-01'
    with pytest.raises(ValueError):questions.validate_updates(run,[answer(run)])


def test_changed_source_invalidates_completed_question_view(run):
    questions.update(run['id'],[answer(run)])
    saved=store.get_run(run['id']);saved['research_sources'][0]['text']='公司尚未签订付费合同。';store.save_run(saved)
    view=questions.snapshot(saved)
    assert view['counts']['partial']==1 and not view['complete']
    assert saved['research_questions'][0]['status']=='supported' # read view preserves history


def test_added_questions_are_deduplicated_and_no_answers_disappear(run):
    questions.update(run['id'],[answer(run)])
    v=questions.plan(run['id'],['有没有独立披露的分项收入？','有没有独立披露的分项收入？'])
    assert v['total']==2 and v['counts']['supported']==1 and not v['complete']


def test_answer_revision_is_idempotent_and_preserves_previous(run):
    questions.update(run['id'],[answer(run)])
    before=store.get_run(run['id'])
    questions.update(run['id'],[answer(run)])
    assert store.get_run(run['id'])['research_questions']==before['research_questions']
    questions.update(run['id'],[answer(run,answer='仅有公司披露，合同真实性需复核。',status='partial',gaps=['独立证据缺失'])])
    q=store.get_run(run['id'])['research_questions'][0]
    assert q['history'][-1]['answer']==before['research_questions'][0]['answer']


def test_report_and_question_answers_saved_together(run):
    draft={'title':'研究','summary':'公司披露付费合同，不能证明回款','stance':'待核实','theses':[
        {'title':title,'body':'合同与回款分别核对','counter_evidence':'公司自述需要复核','invalidate_if':'发现未实际签约','evidence_ids':['official'],'status':'inference'} for title in ('合同','回款')],
        'sections':[{'key':key,'body':'公司自述需要进一步核对','evidence_ids':['official'],'status':'inference'} for key in deep_research.SECTION_TITLES],
        'questions':['合同是否收款？'],'limitations':[],'question_answers':[answer(run)]}
    memo=deep_research.validate_memo(draft,run)
    deep_research.save_memo(run['id'],memo,'测试研究')
    saved=store.get_run(run['id']);assert questions.snapshot(saved)['complete']
    assert saved['memo']['question_answers'][0]['bindings'][0]['quote']=='公司已签订付费合同'


def test_human_api_and_html_export_escape_model_text(run):
    with TestClient(main.app) as client:
        v=client.post('/api/runs/'+run['id']+'/question-answers',json={'answers':[answer(run,answer='<script>alert(1)</script>')]}).json()
        assert v['items'][0]['assessment_origin']=='human'
        output=client.get('/api/runs/'+run['id']+'/export?format=html').text
        assert '<script>' not in output and '&lt;script&gt;' in output and '问题级证据清单' in output


def test_readonly_turn_blocks_question_writes(run):
    c=store.new_conversation(run['id']);store.begin_turn(c['id'],'只读查看，不要修改记录','test','documents')
    with pytest.raises(ValueError,match='只读'):agent.tool(c,'plan_research_questions',{'questions':['收款了吗？']})


def test_conflicting_financial_evidence_does_not_complete(run):
    run['facts']=[{'id':'revenue-2025','label':'营业收入','metric':'revenue','year':2025,'value':'100','status':'conflict','evidence_ids':['official']}]
    saved=questions.validate_updates(run,[answer(run)])[0]
    assert saved['status']=='conflict'


def draft(run,answers=None):
    return {'title':'合同核对','summary':'合同签订与收款分别验证','stance':'继续核对回款',
        'theses':[{'title':name,'body':'原文披露签订合同，回款仍需核对','counter_evidence':'合同不等于收款',
                    'invalidate_if':'取得未实际签约的证据','evidence_ids':['official'],'status':'inference'} for name in ('合同','回款')],
        'sections':[{'key':key,'body':'保留公司披露与研究推断的区别','evidence_ids':['official'],'status':'inference'} for key in deep_research.SECTION_TITLES],
        'questions':['后续核对回款'],'limitations':[],'question_answers':answers or []}


def test_edit_question_keeps_identity_evidence_and_history_but_requires_review(run):
    original=questions.update(run['id'],[answer(run)])['items'][0]
    view=questions.edit(run['id'],original['id'],{'question':'合同是否已经完成回款？','priority':'high'})
    edited=view['items'][0]
    assert edited['id']==original['id'] and edited['bindings']==original['bindings']
    assert edited['answer']==original['answer'] and edited['status']=='partial' and edited['needs_review']
    assert edited['priority']=='high' and not view['complete']
    assert edited['history'][-1]['question']==original['question']
    # A new explicit assessment may clear the semantic review gap.
    checked=questions.update(run['id'],[answer(run,gaps=[],status='partial')])['items'][0]
    assert not checked['needs_review']


def test_priority_archive_and_restore_are_lossless_and_excluded_from_completion(run):
    original=questions.update(run['id'],[answer(run)])['items'][0]
    qid=original['id']
    view=questions.edit(run['id'],qid,{'priority':'low','archived':True})
    assert view['total']==0 and view['archived_count']==1 and not view['complete']
    assert view['items'][0]['bindings']==original['bindings']
    with pytest.raises(ValueError,match='归档'): questions.update(run['id'],[answer(run)])
    view=questions.edit(run['id'],qid,{'archived':False})
    assert view['complete'] and view['total']==1 and view['items'][0]['priority']=='low'
    assert view['items'][0]['answer']==original['answer']


def test_normalized_plan_deduplicates_without_restoring_archived_question(run):
    view=questions.plan(run['id'],['AI 合同回款了吗？','ai合同回款了吗 ?'])
    assert view['total']==2
    qid=view['items'][1]['id']
    questions.edit(run['id'],qid,{'archived':True})
    after=questions.plan(run['id'],['AI合同回款了吗？'])
    assert len(after['items'])==2 and after['archived_count']==1


def test_edit_does_not_make_original_question_id_collide_with_new_plan(run):
    old=questions.inventory(run)[0]
    questions.edit(run['id'],old['id'],{'question':'合同已经回款了吗？'})
    view=questions.plan(run['id'],[old['question']])
    assert len(view['items'])==2 and len({q['id'] for q in view['items']})==2


@pytest.mark.parametrize('changes',[{'priority':[]},{'archived':'false'},{'question':''},{'bindings':[]}])
def test_question_edit_rejects_invalid_fields_atomically(run,changes):
    before=store.get_run(run['id'])
    with pytest.raises(ValueError):questions.edit(run['id'],questions.inventory(run)[0]['id'],changes)
    assert store.get_run(run['id'])==before


def test_report_sync_tracks_question_answers_and_explicit_update_without_revaluing(run):
    from app import valuation
    valuation.save_result(run['id'],'pb',{'book_equity_100m':'10','multiple':'2'})
    before=store.get_run(run['id'])
    memo=deep_research.validate_memo(draft(before,[answer(run)]),before)
    deep_research.save_memo(run['id'],memo,'初稿')
    first=store.get_run(run['id'])
    assert questions.report_sync(first)['status']=='current'
    questions.update(run['id'],[answer(run,answer='合同已签订，款项尚未验证。',status='partial',gaps=['缺收款凭证'])])
    changed=store.get_run(run['id'])
    sync=questions.report_sync(changed)
    assert sync['needs_update'] and sync['changed_question_ids']==[questions.inventory(run)[0]['id']]
    assert changed['memo']==first['memo'] and changed['report_revision']==first['report_revision']
    revised=deep_research.validate_memo(draft(changed),changed)
    deep_research.save_memo(run['id'],revised,'明确更新报告')
    updated=store.get_run(run['id'])
    assert questions.report_sync(updated)['status']=='current'
    assert updated['memo_revisions'][-1]['memo']==first['memo']
    assert updated['valuation_history']==first['valuation_history']


def test_priority_only_change_does_not_mark_report_outdated(run):
    deep_research.save_memo(run['id'],deep_research.validate_memo(draft(run),run),'初稿')
    questions.edit(run['id'],questions.inventory(run)[0]['id'],{'priority':'high'})
    assert questions.report_sync(store.get_run(run['id']))['status']=='current'


def test_changed_source_even_with_same_quote_marks_question_and_report_for_review(run):
    deep_research.save_memo(run['id'],deep_research.validate_memo(draft(run,[answer(run)]),run),'初稿')
    def change(r):r['research_sources'][0]['text']+='该合同已于次日终止。'
    store.update_run(run['id'],change)
    current=store.get_run(run['id']);stored=copy.deepcopy(current)
    assert questions.snapshot(current)['items'][0]['needs_review']
    sync=questions.report_sync(current)
    assert sync['needs_update'] and sync['source_changed_question_ids']==[questions.inventory(run)[0]['id']]
    assert sync['changed_source_ids']==['official']
    assert current==stored==store.get_run(run['id'])


def test_changed_report_source_without_question_binding_also_marks_report_stale(run):
    deep_research.save_memo(run['id'],deep_research.validate_memo(draft(run),run),'初稿')
    store.update_run(run['id'],lambda r:r['research_sources'][0].update(published_at=None))
    sync=questions.report_sync(store.get_run(run['id']))
    assert sync['needs_update'] and sync['changed_source_ids']==['official']


def test_historical_report_remains_untracked_without_mutating_history(run):
    before=store.get_run(run['id'])
    assert questions.report_sync(before)['status']=='untracked'
    assert store.get_run(run['id'])==before


@pytest.mark.parametrize('change_kind',['question','source'])
def test_concurrent_question_or_source_edit_cannot_be_stamped_as_synchronized(run,change_kind):
    memo=deep_research.validate_memo(draft(run,[answer(run)]),run)
    if change_kind=='question':
        questions.edit(run['id'],questions.inventory(run)[0]['id'],{'completion_criteria':'必须取得银行收款凭证'})
    else:
        store.update_run(run['id'],lambda r:r['research_sources'][0].update(text='公司尚未签订付费合同。'))
    changed=store.get_run(run['id'])
    with pytest.raises(ValueError,match='生成期间'):
        deep_research.save_memo(run['id'],memo,'不得覆盖并发修改')
    assert store.get_run(run['id'])==changed
