"""User-authorized report patches. The model never chooses its own edit scope."""
from copy import deepcopy
from dataclasses import dataclass
import re


EDITORIAL_FIELDS = ('title', 'summary', 'stance', 'theses', 'sections', 'questions', 'limitations', 'tracking_metrics', 'investment_recommendation')
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
             'limitations': ('研究边界', '限制说明'),
             'tracking_metrics': ('持续跟踪指标', '跟踪指标', '跟踪表', '监测指标'),
             'investment_recommendation': ('投资建议', '买卖建议', '投资评级', '决策建议')}
SECTION_FIELDS = ('body', 'evidence_ids', 'status')
THESIS_FIELDS = ('title', 'body', 'evidence_ids', 'counter_evidence', 'invalidate_if', 'status')
EDIT = r'(?:修订|修改|改写|改正|纠正|调整|更新|重写|替换|润色|补充|改为|改成|完善|修正|改(?!善|变))'
LOCAL = re.compile(r'(?:仅|只)(?:需|要|能|允许)?\s*' + EDIT + r'|定点(?:编辑|修订|修改|修正)?|局部(?:修改|编辑|修订)|(?:其余|其他).{0,20}(?:不变|原样)')
PRESERVE = re.compile(r'(?:不要|不再|不得|禁止|无需|不能)\s*' + EDIT + r'|不(?:改|动|修改|更新)|(?:保持|保留).{0,12}(?:不变|原样)|(?:不变|原样保留)')
FULL = re.compile(r'(?:完整|全面|整体|全文|全量|重新).{0,4}(?:重写|改写|修订|更新|生成).{0,5}(?:报告|备忘录)|(?:重写|改写|修订|更新).{0,3}(?:整份|全部|全文)(?:报告|备忘录)?'
                  r'|(?:生成|形成|完成|保存|交付|修复|修订|更新|重写).{0,16}(?:完整|整份|全文)(?:的)?(?:买方|投资研究|深度研究|研究)?(?:报告|备忘录)')
CALCULATION_OBJECT = re.compile(r'(?:已保存|已经保存|已有|已采用|已确认|已计算)(?:的)?估值(?:计算)?(?:结果|输出|参数|假设|情景|方案|模型)?'
                                r'|估值(?:计算结果|计算输出|结果|输出|参数|假设|情景|方案|模型)')


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


def _without_calculation_objects(clause):
    """Protecting saved financial outputs does not freeze a prose section.

    Mask only explicitly qualified objects, retaining other protected fields in
    mixed lists. Bare "估值保持不变" and "已保存估值章节" still protect text.
    """
    def replace(match):
        suffix = clause[match.end():]
        if re.match(r'(?:的)?(?:章节|部分|段落|正文|叙述|论点|文字)', suffix):
            return match.group()
        return '[计算对象]'
    return CALCULATION_OBJECT.sub(replace, clause)


def _calculation_clause(clause):
    """Model assumptions and references to corrected scenarios are not prose edits."""
    # Explicit report text remains editable even when it discusses share counts.
    if FULL.search(clause) or re.search(r'章节|段落|正文|摘要|论点|立场|引用|核实清单|投资建议|买卖建议|报告标题', clause):
        return False
    return _without_calculation_objects(clause) != clause or bool(re.search(r'估值参数|模型参数|其他参数|其余参数|敏感性|股数|股本|估值情景|估值方案|'
                          r'倍数|增长假设|增长轴|倍数轴|折现率|资本成本|(?:重新|不要|不再|不)?重算|'
                          r'(?:以|沿用|采用|按).{0,30}估值.{0,8}(?:基准|计算)|'
                          r'(?:参数|假设).{0,10}(?:保持|保留|不变|原样)|'
                          r'(?:保持|保留).{0,10}(?:参数|假设)', clause))


def _preserved_paths(clauses,memo):
    paths=set()
    for clause in clauses:
        if not PRESERVE.search(clause): continue
        clause = _without_calculation_objects(clause)
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
    clauses = [re.sub(r'(?:其他|其余)?(?:估值|模型)?(?:参数|假设)(?:均|全部|都)?(?:保持|保留)?(?:不变|原样)', '', c)
               if FULL.search(c) else c for c in clauses]
    # A financial instruction such as "其他参数保持不变" must not turn a full
    # unpublished-draft recovery into a patch against the old published brief.
    clauses = [c for c in clauses if not _calculation_clause(c)]
    editorial_text = '。'.join(clauses)
    active = [c for c in clauses if not PRESERVE.search(c)]
    preserved=_preserved_paths(clauses,memo)
    if any(FULL.search(re.split(r'改为|改成|替换为|更新为|调整为',c,maxsplit=1)[0]) for c in active) and not LOCAL.search(editorial_text):
        return EditScope(tuple(sorted(_all_paths(memo)-preserved))) if preserved else EditScope()
    paths = set()
    named_theses = set()
    for clause in active:
        # Names inside factual discussion or preserved prose do not grant writes.
        if not re.search(EDIT, clause):
            continue
        # Replacement content can mention other report fields without granting
        # permission to edit those fields (e.g. 摘要改为“风险仍待核实”).
        clause=re.split(r'改为|改成|替换为|更新为|调整为',clause,maxsplit=1)[0]
        recommendation_detail = bool(re.search(r'(?:投资建议|买卖建议|投资评级|决策建议)(?:中|里)?的', clause))
        section_keys = [key for key, names in SECTION_NAMES.items()
                        if any(_mentions(clause, name) for name in (key,) + names)]
        if recommendation_detail: section_keys = []
        matches = list(re.finditer(r'第\s*(\d+|[一二三四五六七八九十])\s*(?:个|条|项)?(?:核心)?(?:估值|财务|经营|现金流|增长|风险)?论点|theses\s*\[\s*(\d+)\s*\]', clause, re.I))
        indices = []
        for match in matches:
            number = match[1]
            index = int(match[2]) if match[2] is not None else ('一二三四五六七八九十'.index(number) if number in '一二三四五六七八九十' else int(number)-1)
            indices.append(index)
        for index, thesis in enumerate(memo.get('theses', [])):
            if thesis.get('title') and re.search(r'[“"「]' + re.escape(thesis['title']) + r'[”"」]', clause):
                indices.append(index)
        named_theses.update(indices)
        referred_theses = False
        if not indices and re.search(r'核心论点|论点|(?<![A-Za-z_])theses(?!\s*\[)', clause):
            if re.search(r'对应论点|该(?:条)?论点|这(?:一|个|条)论点', clause):
                indices.extend(sorted(named_theses))
                referred_theses = True
            else:
                indices.extend(range(len(memo.get('theses', []))))
        # "第3条估值论点" names a thesis, not a second authorization for the
        # valuation section. An explicit "估值章节/正文" remains independently
        # addressable in the same clause.
        if indices:
            generic = {'valuation': ('估值',), 'risks': ('风险',), 'catalysts': ('催化剂', '催化因素')}
            section_keys = [key for key in section_keys if key not in generic
                            or _mentions(clause, key)
                            or any(_mentions(clause, name) for name in SECTION_NAMES[key] if name not in generic[key])
                            or any(re.search(re.escape(name) + r'(?:章节|部分|段落|正文)', clause) for name in generic[key])]
        for key in section_keys:
            paths.update(f'/sections/{key}/{field}' for field in _fields(clause))
        for index in indices:
            if index < 0 or index >= len(memo.get('theses', [])):
                raise ValueError('指定的核心论点不存在，请按当前报告的论点序号选择编辑范围')
            fields = _fields(clause, thesis=True)
            if referred_theses and fields == THESIS_FIELDS:
                fields = tuple(p.rsplit('/', 1)[-1] for p in paths if p.startswith(f'/theses/{index}/'))
            paths.update(f'/theses/{index}/{field}' for field in fields)
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
    if LOCAL.search(editorial_text) and re.search(EDIT, editorial_text):
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
