"""Synthetic missing-evidence decision for unrelated report pipeline tests."""


def watch_recommendation():
    return {'action': 'watch', 'status': 'insufficient_evidence', 'basis': 'evidence_gap',
        'confidence': 'low', 'summary': '合成测试尚缺可比价格与估值依据，暂不决策。',
        'horizon': '取得缺失证据后重评', 'new_position': '未持仓者等待关键证据。',
        'existing_position': '若已持有，核对原有依据并复核风险。',
        'buy_conditions': ['价格与有效估值可比且现金回收得到支持时再评估买入'],
        'exit_conditions': ['经营披露证明核心论点失效时重评退出'],
        'revisit_triggers': ['取得原始披露、有效行情和已确认估值情景'],
        'key_risks': ['现金回收与披露解释不一致'],
        'blocking_gaps': ['尚缺可比市场价格和估值依据'],
        'evidence_ids': [], 'valuation_scenario_ids': []}
