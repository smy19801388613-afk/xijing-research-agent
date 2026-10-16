"""Durable adversarial-review issues, separate from citation validation."""
from copy import deepcopy
import hashlib
import json


def normalize(review, revision=None):
    result = deepcopy(review) if isinstance(review, dict) else {}
    issues = []
    seen = set()
    for raw in result.get('issues', []):
        item = deepcopy(raw) if isinstance(raw, dict) else {'text': str(raw)}
        text = item.get('text') or item.get('issue') or item.get('claim') or item.get('description')
        if not isinstance(text, str):
            text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        identity = item.get('id') or ('review-' + hashlib.sha256(' '.join(text.split()).encode()).hexdigest()[:16])
        if identity in seen:
            continue
        seen.add(identity)
        item.update(id=identity, text=text,
                    status=item.get('status') if item.get('status') in ('open', 'resolved', 'needs_review') else 'open')
        if revision is not None:
            item.setdefault('first_seen_revision', revision)
        issues.append(item)
    result['schema_version'] = 2
    result['issues'] = issues
    return result


def after_refinement(previous, revision, changed_paths=()):
    result = normalize(previous, revision)
    for issue in result['issues']:
        # No second reviewer was called: editing alone is not a resolution.
        if issue['status'] != 'needs_review':
            issue['previous_status'] = issue['status']
        issue['status'] = 'needs_review'
    result.update(revision_performed=True, independent_review_performed=False,
                  citation_validation='passed', reviewed_revision=previous.get('reviewed_revision') if isinstance(previous, dict) else None,
                  edited_paths=list(changed_paths),
                  semantic_status='本次修订已通过引用校验，未另行调用反方模型复审；已有审阅意见保留为待复核，不表示已经解决。')
    return result


def combined(previous,working):
    """An unpublished review adds issues; it cannot erase published history."""
    result=normalize(previous)
    result['issues']+=normalize(working)['issues']
    return normalize(result)


def after_independent_review(previous, current, revision):
    """A new review does not silently resolve earlier issues it did not mention."""
    prior=after_refinement(previous,revision-1)
    result=normalize(current,revision)
    for issue in result['issues']:
        issue['status']='open'
    result['issues']=normalize({'issues':prior['issues']+result['issues']})['issues']
    result.update(independent_review_performed=True,
                  semantic_status='本次已对生成稿进行反方审阅；既有意见未经逐项复核的保留为待复核，不因本次未提及而视为解决。')
    return result


def issue_text(issue):
    if isinstance(issue,str):
        return '历史意见，待复核：'+issue
    record=normalize({'issues':[issue]})['issues'][0]
    label={'open':'未解决','resolved':'已复核解决','needs_review':'待复核'}[record['status']]
    return label+'：'+record['text']
