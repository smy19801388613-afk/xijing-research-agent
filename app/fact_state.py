"""One derived state contract for legacy facts, model context and delivery.

Never upgrade provenance from prose or from an extra PDF merely being attached.
"""
import re
from .finance import METRICS, number, comparison_view

LABELS={'api_single':'接口单源','document_single':'原文单源','cross_source_match':'跨源数值一致',
    'conflict':'来源冲突','ocr_pending':'OCR待复核','missing':'缺失','unverified':'来源未确认','legacy_match':'历史匹配待复核'}


def verification(fact,evidence=(),as_of=None):
    ids=fact.get('evidence_ids',[])
    sources=[e for e in evidence if e.get('id') in ids]
    state={'api_only':'api_single','document_only':'document_single','verified':'legacy_match','conflict':'conflict',
        'ocr_pending':'ocr_pending','missing':'missing'}.get(fact.get('status'),'unverified')
    if number(fact.get('value')) is None: state='missing'
    elif state!='conflict' and (fact.get('extraction') in ('windows_ocr','ocr') or any(e.get('extraction') in ('windows_ocr','ocr') or e.get('read_status')=='windows_ocr' for e in sources)):
        state='ocr_pending'
    elif state=='legacy_match' and number(fact.get('cross_check_value')) is not None and len({e['id'] for e in sources})>=2 and {'api','document'}.issubset({e.get('kind') for e in sources}):
        from decimal import Decimal
        tolerance={'元':Decimal('.5'),'千元':Decimal(500),'万元':Decimal(5000)}.get(fact.get('original_unit'),Decimal('.5'))
        state='cross_source_match' if abs(number(fact['cross_check_value'])-number(fact['value']))<=tolerance else 'conflict'
    dated=bool(fact.get('announced_date'))
    day=str(fact.get('announced_date') or '')[:10].replace('-','')
    after=bool(as_of and dated and day>as_of.replace('-',''))
    return {'schema_version':1,'state':state,'label':LABELS[state],'evidence_ids':list(ids),
        'locators':[{'evidence_id':e['id'],'kind':e.get('kind'),'document_id':e.get('document_id'),'page':e.get('page'),
            'material_id':e.get('material_id'),'locator':e.get('locator'),'quote':e.get('quote',''),'sha256':e.get('sha256')} for e in sources],
        'source_independence':'not_assessed','historical_eligibility':'after_cutoff' if after else 'known_date' if dated else 'unknown_date',
        'usable_for_calculation':state in ('api_single','document_single','cross_source_match','legacy_match') and not after,
        'limitations':['跨源一致仅表示已有核对记录的数值匹配，不证明来源独立或投资判断正确'] if state=='cross_source_match' else ['单源或未完整核验，不能称双源已核验']}


def fact_view(fact,run):
    from .financial_basis import conflicts
    state=verification(fact,run.get('evidence',[]),run.get('request',{}).get('as_of'))
    issues=[i for i in conflicts(run) if fact['id'] in i['fact_ids']]
    if issues:
        state={**state,'usable_for_calculation':False,'calculation_basis_issues':issues,
               'label':state['label']+' · 财务底稿冲突'}
    return {**fact,'verification':state}


def enrich(run):
    from .financial_basis import conflicts
    run = comparison_view(run)
    issues=conflicts(run)
    blocked={fid:i['message'] for i in issues for fid in i['fact_ids']}
    for calc in run['calculations']:
        reasons=list(dict.fromkeys(blocked[fid] for fid in calc.get('inputs',[]) if fid in blocked))
        if reasons:
            calc.update(value=None,change=None,reason='；'.join(reasons),basis_conflict=True)
    if issues:
        from .finance import comparison_check
        run['checks']=[comparison_check(run['calculations']) if c.get('kind')=='calculation'
                       and c.get('label')=='核心同比计算' else c for c in run.get('checks',[])]
    run['facts']=[fact_view(f,run) for f in run.get('facts',[])]
    if issues: run['financial_basis_issues']=issues
    return run


def claims(text,run,reference_ids=()):
    """Bounded wording check, not a general financial semantic verifier."""
    issues=[]
    for sentence in re.split(r'[。；;\n]',text):
        # Negative/conditional wording is a disclosure of the boundary.
        positive=re.sub(r'(?:尚未|未|不|不能|不得|并非|不是|尚不|待).{0,4}(?:已核验|已验证|经核验|双源(?:一致|核验|验证)|跨源(?:数值)?(?:一致|核验|验证))','',sentence)
        positive=re.sub(r'(?:不能|不得|不要|不应)(?:将|把|称|声称).{0,20}(?:已核验|已验证|双源(?:一致|核验|验证))','',positive)
        high=bool(re.search(r'已核验|已验证|经核验|双源(?:一致|核验|验证)|跨源(?:数值)?(?:一致|核验|验证)',positive))
        if not high: continue
        matches=[]
        years={int(y) for y in re.findall(r'20\d{2}',sentence)}
        for fact in run.get('facts',[]):
            names=METRICS.get(fact.get('metric'),{}).get('aliases',[])+[fact.get('label','')]
            named=any(n and n in sentence for n in names)
            cited=fact['id'] in reference_ids or fact['id'] in sentence or bool(set(fact.get('evidence_ids',[]))&set(reference_ids))
            if (named or cited) and (not years or fact.get('year') in years): matches.append(fact)
        if not matches and re.search(r'财务(?:事实|数据|指标)|这些(?:事实|数据)|全部数据',sentence): matches=run.get('facts',[])
        for fact in matches:
            state=verification(fact,run.get('evidence',[]),run.get('request',{}).get('as_of'))
            if state['state']!='cross_source_match' or state['historical_eligibility']=='after_cutoff':
                issues.append({'type':'provenance_overclaim','fact_id':fact['id'],'claim':sentence[:350],
                    'message':fact.get('label',fact['id'])+'实际状态为'+state['label']+'，请保持状态或明确只是确定性计算/探索情景。'})
    return issues


def assert_claims(text,run,reference_ids=()):
    issues=claims(text,run,reference_ids)
    if issues: raise ValueError('事实状态校验未通过：'+'；'.join(i['message'] for i in issues[:5]))
