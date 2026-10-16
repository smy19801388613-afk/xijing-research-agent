"""User-authorized report patches. The model never chooses its own edit scope."""
from copy import deepcopy
from dataclasses import dataclass
import re


EDITORIAL_FIELDS = ('title', 'summary', 'stance', 'theses', 'sections', 'questions', 'limitations')
SECTION_NAMES = {
    'business': ('公司靠什么赚钱', '商业模式', '公司业务', '业务章节'),
    'industry': ('竞争与行业位置', '行业与竞争', '行业竞争', '行业章节'),
    'financial_quality': ('经营质量与现金回收', '财务质量', '经营质量', '现金回收章节'),
    'valuation': ('估值、预期与安全边际', '估值章节', '估值部分', '估值段落', '估值'),
    'catalysts': ('催化剂与跟踪信号', '催化剂', '催化因素'),
    'risks': ('风险与反方观点', '风险章节', '风险部分', '风险段落', '风险'),
    'research_plan': ('下一步验证什么', '研究计划', '后续验证计划'),
}
TOP_NAMES = {'title': ('标题', '报告名称'), 'summary': ('摘要', '核心结论'),
             'stance': ('研究立场',), 'questions': ('核实清单',),
             'limitations': ('研究边界', '限制说明')}
SECTION_FIELDS = ('body', 'evidence_ids', 'status')
THESIS_FIELDS = ('title', 'body', 'evidence_ids', 'counter_evidence', 'invalidate_if', 'status')
EDIT = r'(?:修订|修改|改写|改正|纠正|调整|更新|重写|替换|润色|补充|改为|改成|完善|修正|改(?!善|变))'
LOCAL = re.compile(r'(?:仅|只)(?:需|要|能|允许)?\s*' + EDIT + r'|定点(?:编辑|修订|修改|修正)?|局部(?:修改|编辑|修订)|(?:其余|其他).{0,20}(?:不变|原样)')
PRESERVE = re.compile(r'(?:不要|不再|不得|禁止|无需|不能)\s*' + EDIT + r'|不(?:改|动|修改|更新)|(?:保持|保留).{0,12}(?:不变|原样)|(?:不变|原样保留)')
FULL = re.compile(r'(?:完整|全面|整体|全文|全量|重新).{0,4}(?:重写|改写|修订|更新|生成).{0,5}(?:报告|备忘录)|(?:重写|改写|修订|更新).{0,3}(?:整份|全部|全文)(?:报告|备忘录)?')


@dataclass(frozen=True)
class EditScope:
    paths: tuple[str, ...] = ()

    @property
    def limited(self):
        return bool(self.paths)


def user_requests(chat):
    """Use server-held user turns, never a tool's model-authored question argument."""
    current = (chat.get('turn_contexts') or {}).get(chat.get('turn_id'), {})
    from .task_contract import current_contract
    contract=current_contract(chat)
    turns = contract.get('obligation_turn_ids') or current.get('task_turn_ids') or [chat.get('turn_id')]
    requests = []
    for turn in turns:
        texts = [m.get('content', '') for m in chat.get('messages', [])
                 if m.get('role') == 'user' and m.get('turn_id') == turn]
        if not texts:
            texts = [(chat.get('turn_contexts') or {}).get(turn, {}).get('user_text', '')]
        for text in texts:
            if isinstance(text, str) and text.strip() and text.strip() not in requests:
                requests.append(text.strip())
        if turn == chat.get('turn_id'):
            break
    if not requests and current.get('original_request'):
        requests.append(current['original_request'])
    return requests


def _mentions(text, name):
    return bool(re.search(r'(?<![A-Za-z_])' + re.escape(name) + r'(?![A-Za-z_])', text))


def _fields(clause, thesis=False):
    named = set()
    for pattern, field in ((r'正文|body', 'body'), (r'引用|证据ID|evidence_ids', 'evidence_ids'),
                           (r'状态|status', 'status')):
        if re.search(pattern, clause):
            named.add(field)
    if thesis:
        for pattern, field in ((r'标题|title', 'title'), (r'反证|替代解释|counter_evidence', 'counter_evidence'),
                               (r'失效条件|invalidate_if', 'invalidate_if')):
            if re.search(pattern, clause):
                named.add(field)
    elif '标题' in clause and '报告标题' not in clause:
        raise ValueError('章节标题由固定目录定义；可编辑报告标题，或指定章节正文的修改')
    if named:
        return tuple(sorted(named))
    if re.search(r'一句|一处|该句|这句|时态|措辞|错字|日期|数字|数值|公式|同比', clause):
        return ('body',)
    return THESIS_FIELDS if thesis else SECTION_FIELDS


def _all_paths(memo):
    paths={'/'+key for key in TOP_NAMES}
    paths.update(f'/sections/{s["key"]}/{field}' for s in memo.get('sections',[]) for field in SECTION_FIELDS)
    paths.update(f'/theses/{index}/{field}' for index in range(len(memo.get('theses',[]))) for field in THESIS_FIELDS)
    return paths


def _preserved_paths(clauses,memo):
    paths=set()
    for clause in clauses:
        if not PRESERVE.search(clause): continue
        subfields=[]
        for pattern,field in ((r'引用|证据ID|evidence_ids','evidence_ids'),(r'状态|status','status'),(r'正文|body','body')):
            if re.search(pattern,clause): subfields.append(field)
        found_section=False
        for key,names in SECTION_NAMES.items():
            if any(_mentions(clause,name) for name in (key,)+names):
                found_section=True
                paths.update(f'/sections/{key}/{field}' for field in subfields or SECTION_FIELDS)
        for key,names in TOP_NAMES.items():
            if any(_mentions(clause,name) for name in (key,)+names): paths.add('/'+key)
        if re.search(r'核心论点|theses',clause):
            paths.update(f'/theses/{index}/{field}' for index in range(len(memo.get('theses',[]))) for field in subfields or THESIS_FIELDS)
        elif not found_section and subfields and not re.search(r'其余|其他',clause):
            paths.update(path for path in _all_paths(memo) if path.rsplit('/',1)[-1] in subfields)
    return paths


def _scope_for_text(text, memo):
    clauses = [v.strip() for v in re.split(r'[，,。！？；;\n]|但是|但(?=其他|其余|不要|不改)', text) if v.strip()]
    active = [c for c in clauses if not PRESERVE.search(c)]
    preserved=_preserved_paths(clauses,memo)
    if any(FULL.search(re.split(r'改为|改成|替换为|更新为|调整为',c,maxsplit=1)[0]) for c in active) and not LOCAL.search(text):
        return EditScope(tuple(sorted(_all_paths(memo)-preserved))) if preserved else EditScope()
    paths = set()
    for clause in active:
        # Names inside factual discussion or preserved prose do not grant writes.
        if not re.search(EDIT, clause):
            continue
        # Replacement content can mention other report fields without granting
        # permission to edit those fields (e.g. 摘要改为“风险仍待核实”).
        clause=re.split(r'改为|改成|替换为|更新为|调整为',clause,maxsplit=1)[0]
        section_keys = [key for key, names in SECTION_NAMES.items()
                        if any(_mentions(clause, name) for name in (key,) + names)]
        for key in section_keys:
            paths.update(f'/sections/{key}/{field}' for field in _fields(clause))
        matches = list(re.finditer(r'第\s*(\d+|[一二三四五六七八九十])\s*(?:个|条|项)?(?:核心)?论点|theses\s*\[\s*(\d+)\s*\]', clause, re.I))
        indices = []
        for match in matches:
            number = match[1]
            index = int(match[2]) if match[2] is not None else ('一二三四五六七八九十'.index(number) if number in '一二三四五六七八九十' else int(number)-1)
            indices.append(index)
        for index, thesis in enumerate(memo.get('theses', [])):
            if thesis.get('title') and re.search(r'[“"「]' + re.escape(thesis['title']) + r'[”"」]', clause):
                indices.append(index)
        if not indices and re.search(r'核心论点|论点|(?<![A-Za-z_])theses(?!\s*\[)', clause):
            indices.extend(range(len(memo.get('theses', []))))
        for index in indices:
            if index < 0 or index >= len(memo.get('theses', [])):
                raise ValueError('指定的核心论点不存在，请按当前报告的论点序号选择编辑范围')
            paths.update(f'/theses/{index}/{field}' for field in _fields(clause,thesis=True))
        # A section/thesis title is not the report title.
        for key, names in TOP_NAMES.items():
            if key == 'title' and (section_keys or indices) and '报告标题' not in clause:
                continue
            if any(_mentions(clause, name) for name in (key,) + names):
                paths.add('/' + key)
    if paths:
        paths-=preserved
        if not paths:
            raise ValueError('报告编辑要求同时包含修改和保留同一字段，请明确哪些字段可以改动')
        return EditScope(tuple(sorted(paths)))
    if preserved:
        if any(re.search(EDIT+r'.{0,8}(?:报告|备忘录)',c) for c in active):
            return EditScope(tuple(sorted(_all_paths(memo)-preserved)))
        raise ValueError('当前只有保留字段的要求，尚未明确允许修改的报告范围')
    if LOCAL.search(text) and re.search(EDIT, text):
        # Continuation instructions without new content inherit the earlier scope.
        if re.search(r'继续|接续|恢复', text) and not re.search(r'改为|改成|替换为|仅修改|只修改', text):
            return None
        raise ValueError('定点编辑范围尚不明确，请指定报告章节、摘要、标题或第几个核心论点；不会扩大为整稿重写')
    return None


def resolve_scope(chat, memo):
    scope = EditScope()
    for text in user_requests(chat):
        selected = _scope_for_text(text, memo)
        if selected is not None:
            scope = selected
    return scope


def _get(memo, path):
    parts = path.strip('/').split('/')
    if len(parts) == 1:
        return memo.get(parts[0])
    if parts[0] == 'sections':
        item = next((s for s in memo.get('sections', []) if s.get('key') == parts[1]), None)
    else:
        items = memo.get('theses', [])
        item = items[int(parts[1])] if int(parts[1]) < len(items) else None
    if item is None:
        raise ValueError('当前报告不包含请求编辑的章节或论点：' + path)
    return item.get(parts[2])


def _put(memo, path, value):
    parts = path.strip('/').split('/')
    if len(parts) == 1:
        memo[parts[0]] = deepcopy(value)
    elif parts[0] == 'sections':
        next(s for s in memo['sections'] if s.get('key') == parts[1])[parts[2]] = deepcopy(value)
    else:
        memo['theses'][int(parts[1])][parts[2]] = deepcopy(value)


def editable_values(memo, scope):
    return {path: deepcopy(_get(memo, path)) for path in scope.paths}


def project_working_draft(published, draft, scope):
    """A legacy full draft may contribute only currently authorized fields."""
    result = deepcopy(published)
    if draft:
        for path in scope.paths:
            value = _get(draft, path)
            if value is not None:
                _put(result, path, value)
    result['question_answers'] = []
    return result


def apply_patch(base, response, scope):
    if not isinstance(response, dict) or set(response) != {'edits'} or not isinstance(response['edits'], list):
        raise ValueError('定点编辑只能返回 {edits:[{path,value}]}，不能返回整份报告或研究问题答案')
    if not response['edits'] or len(response['edits']) > len(scope.paths):
        raise ValueError('定点编辑需要至少一个修改，且不能超过授权路径数')
    result = deepcopy(base)
    seen = set()
    for edit in response['edits']:
        if not isinstance(edit, dict) or set(edit) != {'path', 'value'}:
            raise ValueError('每项定点修改必须且只能包含 path、value')
        path = edit['path']
        if not isinstance(path, str) or path not in scope.paths or path in seen:
            raise ValueError('定点编辑包含未授权或重复路径：' + str(path))
        seen.add(path)
        _get(result, path)
        _put(result, path, edit['value'])
    result['question_answers'] = []
    return result


def preserve_unauthorized(published, candidate, validated, scope):
    """Keep editorial bytes outside scope; retain validator-created provenance."""
    result = deepcopy(validated)
    content = deepcopy(published)
    for path in scope.paths:
        _put(content, path, _get(validated, path))
        if path.startswith('/theses/') and path.endswith('/evidence_ids'):
            index=int(path.split('/')[2])
            if 'fact_ids' in validated['theses'][index]:
                content['theses'][index]['fact_ids']=deepcopy(validated['theses'][index]['fact_ids'])
    for field in EDITORIAL_FIELDS:
        if field in content:
            result[field] = deepcopy(content[field])
        else:
            result.pop(field, None)
    result['question_answers'] = []
    # The actual model merge must also preserve everything outside the whitelist.
    expected = deepcopy(published)
    for path in scope.paths:
        _put(expected, path, _get(candidate, path))
    if any(candidate.get(k) != expected.get(k) for k in EDITORIAL_FIELDS):
        raise ValueError('定点编辑改变了授权范围以外的报告字段')
    return result
