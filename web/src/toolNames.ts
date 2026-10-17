export const toolNames:Record<string,string>={
  plan_research_questions:'建立研究问题',update_research_question:'保存问题证据',
  search_company:'查找公司',start_research:'建立财务底稿',fetch_annual_report:'获取公开年报',
  fetch_market_quote:'取得市场报价',attach_document:'接入研究材料',read_document:'查阅 PDF 原文',
  search_documents:'检索披露原文',web_search:'联网搜索',search_web:'联网搜索',
  read_webpage:'阅读网页',read_public_pdf:'查阅公开公告 PDF',read_web_page:'阅读网页',read_web:'阅读网页',
  build_investment_memo:'撰写深度研究',refine_investment_memo:'依据原文修订报告',
  valuation_options:'比较估值方法',calculate_valuation:'计算估值情景',propose_valuation:'提出估值方案',
  calculate_proposal:'计算选定方案',revise_report:'修订研究报告',
  record_financial_periods:'保存财务期间底稿',analyze_financial_periods:'分析财务期间',
  calculate_sensitivity:'计算估值敏感性',save_comparables:'整理可比公司倍数',
  get_tracking_metrics:'查看跟踪指标',record_tracking_observation:'记录跟踪观察',
};

export function toolLabel(tool:string,label?:string){
  return toolNames[tool]||(label&&/[\u3400-\u9fff]/.test(label)?label:'研究步骤');
}
