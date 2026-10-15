"""Reject clearly elapsed dates presented as upcoming catalysts.

This is a narrow temporal consistency check, not an event calendar. A fiscal
year in an annual meeting/report name does not determine its publication date.
"""
import calendar
from datetime import date
import re


def past_catalysts(candidate, as_of):
    try:
        cutoff = date.fromisoformat(str(as_of))
    except ValueError:
        return []
    sections = candidate.get('sections', [])
    if isinstance(sections, dict):
        sections = [dict(item, key=key) if isinstance(item, dict) else {'key':key, 'body':item}
                    for key, item in sections.items()]
    issues = []
    patterns = [
        (re.compile(r'(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)'), 'date'),
        (re.compile(r'(20\d{2})年\s*(\d{1,2})月(?:\s*(\d{1,2})日)?'), 'month'),
        (re.compile(r'(20\d{2})年?\s*春节'), 'spring'),
    ]
    historical = re.compile(r'回顾|历史|过去|此前|当时|已(?:经|于|在|完成|结束|发生|公布|披露|实施|兑现|通过)|并非未来|不是未来|不再是未来')
    for section in sections if isinstance(sections, list) else []:
        if not isinstance(section, dict) or section.get('key') not in ('catalysts', 'catalyst'):
            continue
        for sentence in re.split(r'[。；;\n]', str(section.get('body') or '')):
            for pattern, kind in patterns:
                for match in pattern.finditer(sentence):
                    try:
                        year = int(match[1])
                        if kind == 'spring':
                            # A generous upper bound; no lunar date is invented.
                            latest = date(year, 3, 31)
                        else:
                            month = int(match[2])
                            latest = date(year, month, int(match[3]) if match[3] else calendar.monthrange(year, month)[1])
                    except (ValueError, IndexError):
                        continue
                    if latest >= cutoff:
                        continue
                    window = sentence[max(0, match.start()-24):match.end()+24]
                    if historical.search(window):
                        continue
                    issues.append(f'催化剂“{match.group()}”早于研究截止日 {cutoff.isoformat()}，不能列为未来事件；若用于复盘请明确历史属性，未来安排需重新核对，不能直接顺延年份。')
    return list(dict.fromkeys(issues))


def assert_catalyst_dates(candidate, run):
    issues = past_catalysts(candidate, run.get('request', {}).get('as_of'))
    if issues:
        raise ValueError('报告时点校验未通过：' + '；'.join(issues[:4]))
