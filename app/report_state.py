"""Read-only report/scenario relationships; never rewrite an earlier report."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from .valuation import same_result


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def report_identity(run):
    """The published output, independent of source/question dependencies."""
    return {'revision': run.get('report_revision'), 'memo_hash': _hash(run.get('memo'))}


def assert_report_base(run, expected):
    if expected != report_identity(run):
        raise ValueError('报告生成期间已有其他修订保存，当前报告未被覆盖；请基于最新报告重新应用本次修改')


def _scenario_value(value):
    if isinstance(value, dict): return {k: _scenario_value(v) for k, v in value.items()}
    if isinstance(value, list): return [_scenario_value(v) for v in value]
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        try:
            n = Decimal(str(value))
            if n.is_finite(): return format(n.normalize(), 'f')
        except InvalidOperation: pass
    return value


def dependency_snapshot(run, memo=None):
    """Shared semantic dependencies for report freshness and saved-step reuse.

    An unrelated new article is not a report input until cited or bound to a
    research question. Facts/calculations and latest scenarios are explicit
    research inputs; duplicate scenario IDs and operational settings are not.
    """
    from . import questions
    memo = memo if memo is not None else run.get('memo') or {}
    snapshot = questions._question_snapshot(run, memo)
    for item in snapshot['questions'].values(): item.pop('revision', None)
    latest = {v['method']: v for v in run.get('valuation_history', [])}
    from .research_extensions import dependencies
    extensions = dependencies(run)
    return {'schema_version': 1, 'question_evidence': snapshot,
            **({'research_extensions': extensions} if extensions else {}),
            'request': {k: run.get('request', {}).get(k) for k in ('ticker', 'year', 'as_of', 'document_id')},
            'facts': [{k: v for k, v in f.items() if k != 'verification'} for f in run.get('facts', [])],
            'calculations': deepcopy(run.get('calculations', [])),
            'valuations': {method: _scenario_value({k: v.get(k) for k in
                ('method', 'assumptions', 'fact_ids', 'output', 'formula')}) for method, v in latest.items()}}


def dependency_fingerprint(run, memo=None):
    return _hash(dependency_snapshot(run, memo))


def valuation_snapshot(run):
    """Capture the scenarios available to a generation call, before model latency."""
    return {"captured_at": run.get("updated_at"),
            "scenarios": deepcopy(run.get("valuation_history", []))}


def referenced_valuation_ids(memo):
    ids = [i for item in memo.get("sections", []) + memo.get("theses", [])
           for i in item.get("evidence_ids", []) if isinstance(i, str)]
    ids += [s.get("id", "") for s in memo.get("sources", []) if s.get("kind") == "valuation"]
    recommendation = memo.get('investment_recommendation') or {}
    ids += recommendation.get('evidence_ids', [])
    ids += ['valuation-' + i.removeprefix('valuation-') for i in recommendation.get('valuation_scenario_ids', []) if isinstance(i, str)]
    return list(dict.fromkeys(i.removeprefix("valuation-") for i in ids if i.startswith("valuation-")))


def link_report_valuations(memo, snapshot):
    linked = deepcopy(memo)
    linked["valuation_context"] = {"schema_version": 1, **deepcopy(snapshot),
        "referenced_ids": referenced_valuation_ids(memo)}
    return linked


def _scenario_basis_conflicts(run, scenario, issues):
    """Match an existing scenario's metric/year, not every issue in the run."""
    from .valuation import CATALOG
    metric = next((m.get('metric') for m in CATALOG if m['id'] == scenario.get('method')), None)
    if not metric:
        return []
    def year(value):
        return int(str(value)) if re.fullmatch(r'\d{4}', str(value)) else None
    explicit_year = year(scenario.get('base_year'))
    years = {explicit_year} if explicit_year is not None else set()
    by_id = {f.get('id'): f for f in run.get('facts', [])}
    for sid in scenario.get('fact_ids', []):
        fact = by_id.get(sid, {})
        fact_year = year(fact.get('year')) if fact.get('metric') == metric else None
        if fact_year is not None:
            years.add(fact_year)
        elif not fact:
            # Historical annual IDs retain their period even if the active fact
            # row is unavailable. Never infer a year from an arbitrary suffix.
            match = re.fullmatch(re.escape(metric) + r'-(\d{4})', str(sid))
            if match: years.add(int(match.group(1)))
    if not years:
        # Older scenarios may predate fact-ID linkage; use their research basis
        # only when no more specific saved input year is available.
        fallback = year(run.get('request', {}).get('year'))
        if fallback is not None: years.add(fallback)
    return [i for i in issues if i.get('metric') == metric and year(i.get('year')) in years]


def report_valuation_status(run):
    """Compare each method with references, falling back to frozen generation inputs.

    IDs locate scenarios; semantic comparison prevents equivalent resubmissions from
    creating stale-report warnings. A matching version is still blocked when its
    financial basis conflicts. Legacy reports without links stay uncertain.
    """
    memo = run.get("memo")
    result = {"state": "no_report", "message": "尚无报告", "basis": "unknown",
              "report_revision": run.get("report_revision"), "items": []}
    if not memo:
        return result
    history = run.get("valuation_history", [])
    if not history:
        return {**result, "state": "no_valuations", "message": "尚未保存估值情景"}
    context = memo.get("valuation_context") or {}
    has_snapshot = context.get("schema_version") == 1 and isinstance(context.get("scenarios"), list)
    snapshot = context.get("scenarios", []) if has_snapshot else []
    catalog = {v["id"]: v for v in history}
    catalog.update({v["id"]: v for v in snapshot})
    reference_ids = referenced_valuation_ids(memo)
    if has_snapshot:
        reference_ids = list(dict.fromkeys(reference_ids + context.get("referenced_ids", [])))
    references = [catalog[i] for i in reference_ids if i in catalog]
    latest = {v["method"]: v for v in history}
    from .financial_basis import conflicts
    basis_issues = conflicts(run)
    result["basis"] = "references" if references else "snapshot" if has_snapshot else "unknown"
    for method, current in latest.items():
        reported = [v for v in references if v["method"] == method]
        if not reported and has_snapshot:
            reported = [v for v in snapshot if v["method"] == method][-1:]
        if reported:
            state = "current" if any(same_result(v, current) for v in reported) else "changed"
        else:
            state = "changed" if has_snapshot else "unlinked"
        blocked = {}
        for scenario in [current, *reported]:
            for issue in _scenario_basis_conflicts(run, scenario, basis_issues):
                blocked[(issue['metric'], issue['year'])] = issue
        result["items"].append({"method": method, "method_name": current.get("method_name", method.upper()),
            "state": 'blocked' if blocked else state, "scenario_match": state,
            "blocking_issues": list(blocked.values()),
            "reported_ids": [v["id"] for v in reported], "latest_id": current["id"],
            "latest_created_at": current.get("created_at"),
            "reported_assumptions": [v.get("assumptions", {}) for v in reported],
            "latest_assumptions": current.get("assumptions", {})})
    changed = [i["method_name"] for i in result["items"] if i["scenario_match"] == "changed"]
    unlinked = any(i["scenario_match"] == "unlinked" for i in result["items"])
    if changed:
        result.update(state="changed", message="报告尚未纳入最新 " + "、".join(changed) + " 情景，请对照假设；原报告内容保留。")
        if unlinked:
            result["message"] += "其他方法缺少完整关联，无法确认是否已纳入。"
    elif unlinked:
        result.update(state="unlinked", message="旧报告未记录完整估值关联，无法确认是否纳入当前情景，请对照。")
    else:
        result.update(state="current", message="当前情景与报告引用或生成时快照一致；快照不代表报告已逐项讨论。")
    result['scenario_match'] = result['state']
    blocked = [i for i in result['items'] if i['state'] == 'blocked']
    if blocked:
        result.update(state='blocked', message='报告引用或最新 ' + '、'.join(i['method_name'] for i in blocked)
            + ' 情景的财务基准存在冲突，暂不能作为有效估值依据；请核对来源后重算并重新评估。原报告和情景记录保留。')
        result['blocking_issues'] = list({(issue['metric'], issue['year']): issue
            for item in blocked for issue in item['blocking_issues']}.values())
    return result
