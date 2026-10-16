"""User limits and observed coverage, independent of model-authored tool arguments."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from .tool_capabilities import TOOLS
from .evidence_policy import eligibility

NEG = r'(?:不要|不得|不能|不用|无需|不必|禁止|别|暂不|先不|不再|不)'
REPORT = r'(?:报告|研报|底稿|备忘录|研究结果)'
DEEP_RESEARCH = r'(?:联网|上网)?(?:深研|深度研究)'
NO_DEEP_RESEARCH = NEG + r'\s*(?:再)?\s*(?:进行|做|启动|开展|使用)?\s*' + DEEP_RESEARCH
PER_SHARE = r'每股(?:价值|估值|价格)'
NO_PER_SHARE = NEG + r'\s*(?:再)?\s*(?:计算|输出|给出|给|生成|显示)\s*' + PER_SHARE
KEEP_VALUATION_INPUTS = NEG + r'\s*(?:再)?(?:调整|修改|更改|改变|变更|改)(?:已保存的|现有的|原有的|当前的|任何)?(?:估值)?(?:参数|假设)'
NETWORK_MODIFIERS = r'(?:再|继续|重新|进一步|主动|额外|进行|执行|使用|调用|通过|直接|尝试|反复|重复|自动|自行|另外|需要|允许|准|想|去|做|本轮|本次|当前|现在|在|以|任何形式的?|任何方式的?|任何|新的?|\s)*'
NETWORK_EVIDENCE = r'(?!(?:联网|上网)?(?:搜索|检索)?(?:摘要|结果|内容|来源|线索|信息|得到的|获得的|返回的|找到的|到的))'
NO_NETWORK_ACTION = NEG + NETWORK_MODIFIERS + NETWORK_EVIDENCE + r'(?:联网|上网|搜索|查新资料|检索新资料|补充新资料)'
VALUATION_METHOD_SUBJECT = (r'(?:(?:DCF|DDM|PB|PE|PS|PEG|SOTP|FCFF|FCFE|Gordon|NAV|EV/(?:EBITDA|EBIT|Sales))(?:\s*(?:模型|方法|估值法))?'
    r'|(?:市盈率|市净率|市销率|股利贴现|股息贴现|现金流折现|分部估值|相对估值|绝对估值)(?:模型|方法|法)?)')
METHOD_ONLY_DISCUSSION = (r'^\s*(?:(?:对于|关于|至于|对)\s*)?' + VALUATION_METHOD_SUBJECT +
    r'(?:\s*(?:和|与|及|/|、)\s*' + VALUATION_METHOD_SUBJECT + r')*\s*'
    r'(?:仍然?|暂时?|先|本轮|本次|继续)?\s*(?:只|仅)(?:讨论|解释)')
REPORT_COMPLETION_CAUTION = (NEG + r'(?:凭|因为|由于)(?:成功)?(?:生成|保存|更新|修订)' +
    REPORT + r'(?:就|便)(?:称|声称|认为|认定|视为|表示|说明)|' +
    NEG + r'(?:把|将)(?:生成|保存|更新|修订)' + REPORT + r'(?:视为|当作|当成|等同于)')


def latest_user(chat):
    return next((m.get('content', '') for m in reversed(chat.get('messages', [])) if m.get('role') == 'user'), '')


def requires_body_read(text):
    """Require a new read, not a reference to material already inspected."""
    verbs = r'(?:打开|读取|阅读|查阅|核对)'
    for clause in re.split(r'[。；;\n，,、]', text):
        for match in re.finditer(verbs + r'.{0,10}(?:正文|原文|全文)', clause):
            prefix = clause[:match.start()]
            if re.search(NEG + r'.{0,8}$', prefix):
                continue
            # "其他已核对的正文保持不变" describes completed evidence.
            # "请重新核对已读正文" remains an affirmative new action: the
            # completion modifier is after the requested verb, not before it.
            if re.search(r'(?:已经|已|曾经)(?:完成|成功|实际|完整|逐页|逐项)?\s*$', prefix):
                continue
            if re.match(verbs + r'(?:过|完)(?:的|了)', match.group()):
                continue
            return True
    return False


def read_limits(text):
    """Translate explicit reading bans to material kinds and optional pages."""
    limits = []
    for clause in re.split(r'[。；;\n，,]', text):
        for match in re.finditer(NEG + r'\s*(?:再|继续|重新|额外|进一步|另外)?\s*'
                                 r'(?:(?:新搜索|联网搜索)或)?(?:补读|读取|阅读|查阅|打开|读)([^。；;\n，,]*)', clause):
            target = re.split(r'但是|但|不过', match[1])[0]
            kinds = []
            if re.search(r'PDF|年报|年度报告|文档|文件|第\s*\d+\s*页|其他页', target, re.I): kinds.append('pdf')
            if re.search(r'网页|网站|网址', target): kinds.append('web')
            if re.search(r'(?:任何|所有|新的?|其他)(?:材料|资料|原文|正文)', target): kinds = ['pdf', 'web']
            pages = []
            for page in re.finditer(r'第?\s*(\d+)\s*(?:[-—至到]\s*(\d+)\s*)?页', target):
                start, end = int(page[1]), int(page[2] or page[1])
                if 0 < start <= end <= 800: pages.extend(range(start, end + 1))
            docid = re.search(r'(?:document_id\s*[:：=]?\s*|文档\s*)([a-f0-9]{16})', target, re.I)
            url = re.search(r'https?://[^\s）)\]”"，,]+', target)
            for kind in dict.fromkeys(kinds):
                item = {'kind': kind}
                if pages: item['pages'] = sorted(set(pages))
                if docid: item['document_id'] = docid[1]
                if url: item['url'] = url[0]
                if item not in limits: limits.append(item)
    return limits


def constraints(text):
    clauses = re.split(r'[。；;\n，,、]', text)
    no_tools = bool(re.search(r'(?:不要|不用|不使用|不调用|无需|不必|不需要|禁止|别|不得|不能)(?:再)?(?:使用|调用|用)?\s*(?:任何|所有)?\s*工具', text))
    strict_read_only = (bool(re.search(r'只读|(?:不要|禁止|不需要)(?:保存|修改)任何(?:内容|数据|记录|结果)', text))
                        or any(re.search(NEG+r'(?:再)?(?:保存|写入|修改)(?:任何(?:内容|数据|记录|结果)?)?\s*$',c) for c in clauses))
    # "DDM仍仅解释方法" limits one method, not unrelated report/question
    # writes. Remove only the named-method phrase; a separate global discussion
    # or explicit read-only/report ban remains effective.
    discussion_clauses = [re.sub(METHOD_ONLY_DISCUSSION, '', c, flags=re.I) for c in clauses]
    discussion_only = any(re.search(r'(?:只|仅)(?:讨论|解释)', c) for c in discussion_clauses)
    read_only = strict_read_only or discussion_only
    # A request to discuss methods can explicitly include a draft assumption
    # card. This exception never authorizes calculation or other writes.
    proposal_requested = any(
        match and not re.search(NEG + r'.{0,6}$', c[:match.start()])
        for c in clauses
        for match in [re.search(r'(?:提出|生成|保存|建立|整理|给出).{0,12}(?:方案|卡片)', c)]
    )
    saved_only = bool(re.search(r'(?:只|仅)(?:能|用|依据|根据|使用|基于).{0,24}(?:已存|已保存|已经保存|已有|现有|保存的).{0,8}(?:材料|资料|来源|研究|内容)', text))
    # A ban on the deep-research operation does not prohibit an explicitly
    # requested annual-report download. Independent network bans still apply.
    network_clauses = [re.sub(NO_DEEP_RESEARCH, '', c) for c in clauses]
    # Evidence cautions such as "不把搜索摘要当全文" describe how to use
    # results; only a negated network action disables retrieval itself.
    no_network = saved_only or any(re.search(NO_NETWORK_ACTION, c) for c in network_clauses)
    no_deep_research = any(re.search(NO_DEEP_RESEARCH, c) for c in clauses)
    # Saving an artifact is not evidence that every research question is solved.
    # A caution against that inference does not forbid the save operation.
    report_clauses = [re.sub(REPORT_COMPLETION_CAUTION, '', c) for c in clauses]
    no_report = read_only or any(re.search(NEG + r'.{0,4}(?:生成|保存|更新|修改|修订|改写|重写|改|写).{0,12}' + REPORT, c) or re.search(REPORT + r'.{0,4}' + NEG + r'.{0,4}(?:改|写|保存|更新|修订)', c) for c in report_clauses)
    calculation_clauses = [re.sub(KEEP_VALUATION_INPUTS, '', re.sub(NO_PER_SHARE, '', c)) for c in clauses]
    no_per_share = any(re.search(NO_PER_SHARE, c) for c in clauses)
    awaiting_confirmation = bool(re.search(r'(?:等待|待|等)(?:我|用户)(?:明确)?(?:确认|采用)(?:之后|后)(?:才|再)(?:可|能|实际)?(?:进行)?(?:计算|执行)', text))
    no_calculation = read_only or awaiting_confirmation or any(re.search(NEG + r'.{0,6}(?:计算|重算|估值)', c) for c in calculation_clauses)
    no_valuation = strict_read_only or (discussion_only and not proposal_requested) or any(re.search(NEG + r'(?:做|进行|保存)?估值', c) for c in clauses)
    no_question_writes = read_only or any(re.search(NEG + r'.{0,5}(?:保存|修改|更新|建立|记录).{0,8}(?:问题|研究记录)', c) for c in clauses)
    require_body = requires_body_read(text)
    reading_limits = read_limits(text)
    no_pdf_read = any(x == {'kind':'pdf'} for x in reading_limits)
    no_web_read = any(x == {'kind':'web'} for x in reading_limits)
    match = re.search(r'最多(?:用|使用)?\s*([一二两三四五六七八九\d]+)\s*(?:组|次)(?:联网)?搜索', text)
    number = {'一':1, '二':2, '两':2, '三':3, '四':4, '五':5, '六':6, '七':7, '八':8, '九':9}
    search_limit = (int(match[1]) if match[1].isdigit() else number.get(match[1], 2)) if match else None
    return dict(no_tools=no_tools, saved_only=saved_only, no_network=no_network, no_deep_research=no_deep_research, no_report=no_report,
                no_calculation=no_calculation, no_per_share=no_per_share, no_valuation=no_valuation, no_question_writes=no_question_writes,
                no_material_writes=read_only, no_pdf_read=no_pdf_read, no_web_read=no_web_read,
                read_restrictions=reading_limits, discussion_only=discussion_only,
                require_body=require_body, search_limit=search_limit)


RULE_LABELS = {'no_tools':'不使用工具', 'saved_only':'仅用已有材料', 'no_network':'不联网',
    'no_deep_research':'不启动深度研究',
    'no_per_share':'仅计算总值，不计算每股价值',
    'no_report':'不修改报告', 'no_calculation':'不计算', 'no_valuation':'不做估值',
    'no_question_writes':'不修改研究问题', 'no_material_writes':'不修改接入材料',
    'no_pdf_read':'不补读PDF', 'no_web_read':'不补读网页', 'require_body':'需查阅正文'}
PERSISTENT = r'以后|今后|从现在起|从此|始终|一直|所有后续|后续每轮'


def explicit_permissions(text):
    """Explicit affirmative requests can override a standing restriction this turn."""
    allowed = set()
    for clause in re.split(r'[。；;\n，,、]', text):
        if re.search(r'取消|解除|撤销',clause):
            if re.search(r'所有限制|全部限制',clause): allowed.update(RULE_LABELS)
            if '工具' in clause: allowed.add('no_tools')
            if re.search(r'联网|搜索|已有材料',clause): allowed.update(('no_network','saved_only'))
            if re.search(DEEP_RESEARCH,clause): allowed.add('no_deep_research')
            if re.search(REPORT,clause): allowed.add('no_report')
            if '估值' in clause: allowed.update(('no_calculation','no_valuation'))
            if '计算' in clause: allowed.add('no_calculation')
            if '每股' in clause: allowed.add('no_per_share')
            if re.search(r'问题|研究记录',clause): allowed.add('no_question_writes')
            if re.search(r'材料|文档|附件',clause): allowed.add('no_material_writes')
            if re.search(r'PDF|文档|文件',clause,re.I): allowed.add('no_pdf_read')
            if '网页' in clause: allowed.add('no_web_read')
        if re.search(NEG, clause) and not re.search(r'取消|解除|撤销', clause):
            continue
        if re.search(r'(?:联网|上网|搜索|查新资料)', clause): allowed.update(('no_network','saved_only'))
        if re.search(r'(?:开始|启动|进行|开展|允许|可以|请).{0,8}' + DEEP_RESEARCH, clause): allowed.add('no_deep_research')
        if re.search(r'(?:使用|调用|用).{0,4}工具', clause): allowed.add('no_tools')
        if re.search(r'(?:生成|保存|更新|修改|修订|写入|重写).{0,12}'+REPORT, clause): allowed.add('no_report')
        if re.search(r'(?:计算|重算|执行|进行).{0,10}(?:估值|情景|桥接|FCFF)|计算', clause, re.I): allowed.add('no_calculation')
        if re.search(r'(?:计算|输出|给出|生成|显示).{0,6}' + PER_SHARE, clause): allowed.add('no_per_share')
        if re.search(r'(?:计算|重算|执行|进行|提出|生成|保存).{0,10}(?:估值|情景|方案)', clause): allowed.add('no_valuation')
        if re.search(r'(?:保存|修改|更新|建立|记录).{0,8}(?:问题|研究记录)', clause): allowed.add('no_question_writes')
        if re.search(r'(?:接入|挂载|上传|绑定).{0,8}(?:PDF|文件|材料|文档|附件)', clause,re.I): allowed.add('no_material_writes')
        if re.search(r'(?:阅读|读取|查阅|补读).{0,8}(?:PDF|文件|文档|年报)',clause,re.I): allowed.add('no_pdf_read')
        if re.search(r'(?:阅读|读取|查阅|补读).{0,8}网页',clause): allowed.add('no_web_read')
    return allowed


def resolve_turn_policy(chat, text, inherited=None):
    """Only explicit standing instructions persist; continuation keeps its task policy."""
    persistent = dict(chat.get('persistent_constraints', {}))
    for clause in re.split(r'[。；;\n，,、]', text):
        if not re.search(PERSISTENT + r'|取消.*限制|解除.*限制|撤销.*限制', clause): continue
        for key in explicit_permissions(clause): persistent.pop(key, None)
        if not re.search(r'取消|解除|撤销', clause):
            persistent.update({k:v for k,v in constraints(clause).items() if v and k in RULE_LABELS})
    rules = constraints('') | persistent
    if inherited: rules.update(inherited)
    for key in explicit_permissions(text): rules[key] = False
    current = constraints(text)
    # A request to cancel a named restriction is permission, not a fresh prohibition.
    if re.search(r'取消|解除|撤销', text):
        for key in explicit_permissions(text): current[key] = False
    # Scope-specific bans are cumulative on a pure continuation. Explicit
    # permission for an entire material kind releases its bans. Permission to
    # read one page/document does not release a ban on a different target.
    released = explicit_permissions(text)
    limited_permission = bool(re.search(r'第?\s*\d+\s*(?:[-—至到]\s*\d+\s*)?页|document_id|https?://|文档\s*[a-f0-9]{16}', text, re.I))
    if limited_permission and not re.search(r'取消|解除|撤销', text):
        released = released - {'no_pdf_read', 'no_web_read'}
    retained = [x for x in rules.get('read_restrictions', []) if
                not ('no_pdf_read' in released and x.get('kind') == 'pdf') and
                not ('no_web_read' in released and x.get('kind') == 'web')]
    rules.update({k:v for k,v in current.items() if v is not False and v is not None and v != []})
    rules['read_restrictions'] = list({str(sorted(x.items())):x for x in [*retained,*current['read_restrictions']]}.values())
    labels = [label for key,label in RULE_LABELS.items() if rules.get(key)]
    if rules.get('search_limit') is not None: labels.append(f"最多搜索 {rules['search_limit']} 次")
    return {'scope':'continuation' if inherited is not None else 'turn', 'rules':rules, 'labels':labels,
            'persistent_rules':persistent,
            'persistent_labels':[label for key,label in RULE_LABELS.items() if persistent.get(key)]}


def effective_rules(chat):
    from .task_contract import current_contract
    return current_contract(chat)['rules']


def task_request(chat):
    from .task_contract import current_contract
    return current_contract(chat)['active_request']


def task_steps(chat):
    from .task_contract import current_contract
    ids = set(current_contract(chat)['obligation_turn_ids'])
    return [s for s in chat.get('steps', []) if s.get('turn_id') in ids]


def prohibited_reason(rules, name, arguments=None):
    if rules.get('no_tools'):
        return '本轮用户明确要求不使用工具，未执行此工具。'
    if name not in TOOLS:
        return '未知工具，请使用工具列表中的名称。'
    caps = TOOLS[name]
    if rules.get('no_material_writes') and 'materials_write' in caps:
        return '本轮只读或禁止修改接入材料，未新增研究或挂载文档。'
    if rules.get('no_pdf_read') and 'pdf_read' in caps:
        return '本轮明确不补读PDF，未检索或读取新的PDF内容；可按授权修订已有报告。'
    if rules.get('no_web_read') and 'web_read' in caps:
        return '本轮明确不补读网页，未读取网页正文。'
    if arguments is not None:
        for limit in rules.get('read_restrictions', []):
            same_identity = ((limit.get('url') and canonical_url(arguments.get('url','')) == canonical_url(limit['url']))
                             or (limit.get('document_id') and arguments.get('document_id') == limit['document_id']))
            if limit.get('kind') + '_read' not in caps and not (same_identity and caps & {'pdf_read','web_read'}): continue
            # Aggregate searches/builds can touch an unspecified document. A
            # missing target cannot be used to bypass a specific material ban.
            if limit.get('document_id') and arguments.get('document_id') != limit['document_id']:
                if name not in ('search_documents','build_investment_memo') or arguments.get('document_id'): continue
            if limit.get('url') and canonical_url(arguments.get('url','')) != canonical_url(limit['url']):
                if name != 'build_investment_memo': continue
            if limit.get('pages'):
                page = arguments.get('page')
                if isinstance(page,int):
                    if not set(range(page,page + max(1,int(arguments.get('count',1))))).intersection(limit['pages']): continue
                elif name not in ('search_documents','read_public_pdf','read_webpage','read_web_page','build_investment_memo'):
                    continue
            return '本轮禁止读取该材料或页码范围；请使用允许的材料和页面。'
    if rules.get('no_network') and 'network' in caps:
        return '本轮用户限定只用已有材料或禁止联网，未执行此工具；可查阅已接入材料或说明缺口。'
    if rules.get('no_deep_research') and name == 'build_investment_memo':
        return '本轮用户明确要求暂不启动深度研究；仍可按授权查找年报、读取原文或讨论已有资料。'
    if rules.get('no_report') and 'report' in caps:
        return '本轮用户明确禁止生成或修改报告，未执行保存。'
    if rules.get('no_calculation') and 'calculation' in caps:
        return '本轮用户明确禁止计算或保存估值，未执行计算。'
    if rules.get('no_valuation') and 'valuation' in caps:
        return '本轮用户明确禁止估值，未生成估值方案。'
    if rules.get('no_question_writes') and 'questions' in caps:
        return '本轮只读要求禁止修改研究问题记录。'
    return None


def material_targets(chat, name, arguments, rules):
    """Resolve resource identity from local metadata, never model-supplied aliases.

    The same PDF may be addressed by its public URL or an attached document ID.
    Full-library search touches every attached document even with no ID in args.
    This performs no indexing, file reads, network calls or writes.
    """
    if arguments is None or not rules.get('read_restrictions'):
        return [arguments]
    # Only selectors the actual tool supports can narrow its permission scope.
    # Otherwise a model could add an ignored page/url argument to bypass a ban.
    selectors = {'read_document': {'document_id','page','count'}, 'search_documents': {'document_id'},
                 'read_public_pdf': {'url'}, 'read_webpage': {'url'}, 'read_web_page': {'url'},
                 'build_investment_memo': set()}
    if name in selectors:
        arguments = {k: v for k, v in arguments.items()
                     if k not in {'document_id','url','page','count'} or k in selectors[name]}
    if not any(x.get('url') or x.get('document_id') for x in rules['read_restrictions']):
        return [arguments]
    if name in ('read_document', 'search_documents'):
        from . import store
        if arguments.get('document_id'):
            try: docs = [store.get_document(arguments['document_id'])]
            except KeyError: docs = []  # The actual document tool rejects unknown IDs.
        elif name == 'search_documents':
            from .documents import attached_documents
            docs = attached_documents(chat)
        else:
            docs = []
        targets = []
        for doc in docs:
            target = dict(arguments, document_id=doc['id'])
            # Ignore an invented `url` argument to a local document tool.
            target.pop('url', None)
            if doc.get('source_url'): target['url'] = doc['source_url']
            targets.append(target)
        if not targets and name == 'search_documents' and not arguments.get('document_id'):
            return []
        return targets or [{k: v for k, v in arguments.items() if k != 'url'}]
    if name in ('read_public_pdf', 'read_webpage', 'read_web_page') and arguments.get('url'):
        from . import store
        target = dict(arguments)
        target.pop('document_id', None)
        targets = [target]
        for limit in rules.get('read_restrictions', []):
            if not limit.get('document_id'): continue
            try: doc = store.get_document(limit['document_id'])
            except KeyError: continue
            if doc.get('source_url') and canonical_url(doc['source_url']) == canonical_url(arguments['url']):
                targets.append(dict(target, document_id=doc['id']))
        return targets
    return [arguments]


def enforce(chat, name, arguments=None):
    rules = effective_rules(chat)
    reason = prohibited_reason(rules, name)
    if reason:
        raise ValueError(reason)
    for target in material_targets(chat, name, arguments, rules):
        reason = prohibited_reason(rules, name, target)
        if reason:
            raise ValueError(reason)
    if name == 'web_search' and rules['search_limit'] is not None:
        # The user's task limit also covers searches nested inside report tools,
        # failed requests and continuation turns. Reused results consume no call.
        done = coverage(task_steps(chat))['search_attempts']
        if done >= rules['search_limit']:
            raise ValueError('已达到用户限定的搜索次数，请查阅已找到的正文或说明缺口。')
    return rules


def canonical_url(url):
    p = urlsplit(url)
    return urlunsplit(('https' if p.scheme in ('http','https') else p.scheme, p.netloc.lower(), p.path.rstrip('/'),
                       urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith('utm_')]), ''))


def coverage(steps, text='', saved_sources=(), message='', as_of=None, run=None, require_body=None):
    """A successful call is not necessarily a body: require returned usable text."""
    sources = {}; search_calls = 0; search_results = 0; local_pages = set(); attempts = 0
    search_attempts = 0; telemetry_incomplete = False; expanded = []; seen_events = set()
    for step in steps:
        if step.get('reused_from_step_id'): continue
        if isinstance(step.get('retrieval_events'), list):
            for event in step['retrieval_events']:
                identity = event.get('id')
                if identity and identity in seen_events: continue
                if identity: seen_events.add(identity)
                # The report may fail after successful reading. Individual event
                # states, not the outer tool state, establish what was observed.
                expanded.append(dict(event, tool='web_search' if event.get('kind') == 'search'
                                     else event.get('kind')))
            continue
        result = step.get('result') or {}
        if step.get('tool') == 'build_investment_memo' and (not isinstance(result,dict) or 'observed_sources' not in result):
            if step.get('progress') or step.get('state') in ('failed','cancelled','running'):
                telemetry_incomplete = True
        if step.get('tool') == 'build_investment_memo' and isinstance(result,dict):
            # Old aggregate sources mixed full-page reads with document-search
            # snippets. Their presence cannot establish actual local read pages.
            if any(s.get('url','').startswith('/api/documents/') for s in result.get('observed_sources',[])):
                telemetry_incomplete = True
        expanded.append(step)
    for step in expanded:
        name = step.get('tool'); result = step.get('result') or {}; args = step.get('arguments') or {}
        if not isinstance(result, dict): continue
        # A top-level tool card is created before authorization/limit checks.
        # Only a dispatched request consumes budget. Nested retrieval events
        # begin at dispatch; legacy records without this flag remain countable.
        if name == 'web_search' and step.get('search_dispatched') is False: continue
        if name == 'web_search':
            search_attempts += 1
            if step.get('state') == 'completed': search_calls += 1
            search_results += len(result.get('results', []))
        if name in ('read_webpage','read_web_page','read_public_pdf'): attempts += 1
        if name == 'read_document' and step.get('state') == 'completed' and not result.get('error'):
            local_pages.update(s.get('url') for s in result.get('snippets', [])
                               if s.get('url') and eligibility(dict(s,read_status=s.get('read_status') or s.get('extraction','pdf_text')),run,as_of)['readable_body']
                               and eligibility(s,run,as_of)['historical_eligibility'] != 'after_cutoff')
        rows = result.get('results', []) if name in ('web_search','read_public_pdf') else [result] if name in ('read_webpage','read_web_page') else result.get('observed_sources',[]) if name=='build_investment_memo' else []
        if name=='build_investment_memo':
            search_calls += len(result.get('search_runs',[]))
            search_attempts += len(result.get('search_runs',[]))
            search_results += sum(s.get('result_count',0) for s in result.get('search_runs',[]))
        if name in ('read_webpage','read_web_page','read_public_pdf') and not rows:
            rows = [{'url':args.get('url'), 'read_status':'failed'}]
        for row in rows:
            url = row.get('url') or args.get('url', '')
            if not url: continue
            if not url.startswith(('http://','https://')): continue
            key = canonical_url(url)
            source=dict(row)
            if name=='web_search': source['read_status']='search_snippet'
            elif not source.get('read_status'): source['read_status']=source.get('extraction','body_read')
            policy=eligibility(source,run,as_of)
            body = (name != 'web_search' and step.get('state') == 'completed' and not result.get('error')
                    and result.get('status') not in ('error','excluded','excluded_after_cutoff','needs_ocr','document_link')
                    and policy['readable_body'] and policy['historical_eligibility']!='after_cutoff')
            status = ('body_read' if body else 'pending' if step.get('state') == 'running' else
                      'failed' if name not in ('web_search','build_investment_memo') else 'search_snippet')
            prior = sources.get(key)
            value = {k:row.get(k) for k in ('title','published_at','source_type')}
            value.update(url=url, read_status=status, cited=url in message or key in message,eligibility=policy)
            if prior and prior['read_status'] == 'body_read': continue
            if prior and status == 'search_snippet': continue
            sources[key] = value
    bodies = sum(s['read_status'] == 'body_read' for s in sources.values())
    def evidence_key(url):
        return url if url.startswith('/api/documents/') else canonical_url(url)
    cited_urls = {evidence_key(url) for url in re.findall(r'''(?:https?://|/api/documents/)[^\s<>()\[\]"'，。；]+''', message)}
    read_this_turn = {url for url, source in sources.items() if source['read_status'] == 'body_read'} | local_pages
    reused_urls = {evidence_key(s['url']) for s in saved_sources if s.get('url')
                   and eligibility(s,run,as_of)['readable_body'] and eligibility(s,run,as_of)['historical_eligibility']!='after_cutoff'
                   and evidence_key(s['url']) in cited_urls}
    reused = len(reused_urls - read_this_turn)
    # Inventory is separate from current reading and citation: merely having a
    # stored body does not satisfy this turn's evidence obligation.
    saved_bodies = set(); saved_verified_bodies = set()
    for source in saved_sources:
        if not source.get('url'): continue
        policy = eligibility(source,run,as_of)
        key = evidence_key(source['url'])
        if policy['readable_body'] and policy['historical_eligibility'] != 'after_cutoff':
            saved_bodies.add(key)
            if policy['supports_answer']: saved_verified_bodies.add(key)
    if require_body is None: require_body = constraints(text)['require_body']
    unmet = require_body and not (bodies or local_pages or reused)
    gaps = (['部分检索记录不完整，需核对已保存来源；不能据此断言未搜索或未读取正文。']
            if unmet and telemetry_incomplete else ['本轮尚未取得可用正文；搜索摘要不满足正文查证要求。'] if unmet else [])
    unread = sum(s['read_status'] != 'body_read' for s in sources.values())
    return dict(search_calls=search_calls, search_results=search_results, body_sources=bodies, document_pages=len(local_pages),
                body_attempts=attempts, reused_cited_sources=reused, saved_sources_available=len(saved_sources),
                search_attempts=search_attempts, telemetry_incomplete=telemetry_incomplete,
                saved_body_sources=len(saved_bodies), saved_verified_body_sources=len(saved_verified_bodies),
                unread_sources=unread, failed_sources=sum(s['read_status']=='failed' for s in sources.values()),
                require_body=require_body, status='unknown' if unmet and telemetry_incomplete else 'partial' if unmet else 'observed', gaps=gaps,
                sources=sorted(sources.values(), key=lambda s:(s['read_status']!='body_read',not s['cited'])))
