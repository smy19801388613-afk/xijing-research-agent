"""One permission registry shared by planning and dispatch.

Capabilities describe side effects, including nested work performed by a tool.
Adding a tool requires declaring its capabilities here before it can run.
"""

TOOLS = {
    'search_company': {'network'},
    'start_research': {'network', 'calculation', 'report', 'materials_write'},
    'fetch_annual_report': {'network', 'materials_write'},
    'attach_document': {'materials_write'},
    'search_documents': {'pdf_read'},
    'read_document': {'pdf_read'},
    'valuation_options': set(),
    # Internal input validation does not publish a calculated valuation result.
    'propose_valuation': {'valuation'},
    'calculate_valuation': {'calculation', 'valuation'},
    'calculate_proposal': {'calculation', 'valuation'},
    'fcff_bridge': {'calculation', 'valuation'},
    'peer_comparison': {'network', 'calculation'},
    'revise_report': {'report'},
    'build_investment_memo': {'network', 'report', 'pdf_read', 'web_read'},
    'refine_investment_memo': {'report'},
    'web_search': {'network'},
    'read_webpage': {'network', 'web_read'},
    'read_web_page': {'network', 'web_read'},
    'read_public_pdf': {'network', 'pdf_read'},
    'plan_research_questions': {'questions'},
    'update_research_question': {'questions'},
}

LABELS = {
    'search_company': '匹配公司', 'start_research': '建立财务研究',
    'fetch_annual_report': '接入年报', 'attach_document': '接入已有资料',
    'search_documents': '检索资料', 'read_document': '阅读原文页',
    'valuation_options': '查看估值方法', 'propose_valuation': '整理估值方案',
    'calculate_valuation': '保存估值情景', 'calculate_proposal': '采用估值方案',
    'fcff_bridge': '计算现金流桥接', 'peer_comparison': '查询同行数据',
    'revise_report': '保存报告', 'build_investment_memo': '研究并保存报告',
    'refine_investment_memo': '修订并保存报告', 'web_search': '搜索公开资料',
    'read_webpage': '阅读网页正文', 'read_web_page': '阅读网页正文',
    'read_public_pdf': '阅读公开披露', 'plan_research_questions': '整理研究问题',
    'update_research_question': '记录问题证据',
}
