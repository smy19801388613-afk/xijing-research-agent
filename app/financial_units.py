"""Conservative, source-bound checks for powers-of-ten currency conversion errors.

This is not a general fact extractor. Only clearly labelled currency table rows
and exact inline amounts are compared. Ambiguous periods, estimates, percentages,
per-share amounts, and calculations remain outside this check.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import re


UNITS = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000), "百万元": Decimal(1000000), "亿元": Decimal(100000000),
         "万亿元": Decimal(1000000000000)}
NUMBER = r"[-−－]?\d[\d,，]*(?:\.\d+)?"
AMOUNT = re.compile(r"(?<![\d.,，])(" + NUMBER + r")\s*(万亿元|亿元|百万元|万元|千元|元)(?![/／]?股)")
NUMBERS = re.compile(NUMBER)
TABLE_UNIT = re.compile(r"(?:单位\s*[:：]\s*(?:人民币\s*)?|[（(]\s*人民币\s*)(万亿元|亿元|百万元|万元|千元|元)(?![/／]?股)")
ROW_UNIT = re.compile(r"[（(]\s*(?:人民币\s*)?(万亿元|亿元|百万元|万元|千元|元)\s*[）)]")
YEARS = re.compile(r"(?<!\d)(20\d{2})\s*年?")
SKIP_LABEL = re.compile(r"每\s*(?:\d+\s*)?股|每股|元\s*[/／]\s*股|EPS|比率|比例|占比|同比|毛利率|股息率|[%％]|每吨|单价", re.I)
NON_FACT = re.compile(r"假设|预测|预计|估计|情景|目标价|下一期|未来|测算|推算|假定|若|如果|差额|增量|减少额|增加额|其他公司|其他企业|另一公司|可比公司|同行")
WEAK = {"金额", "合计", "收入", "利润", "本期", "上期", "公司", "年度", "其中", "亿元", "万元",
        "期末", "期初", "现金", "销售", "数据", "报告", "约为", "达到", "合并", "归母",
        "营业", "业务", "渠道", "平台", "线上", "其他", "费用", "净额", "总额", "金流"}


def normalized(value):
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE).replace("直营", "直销").lower()


def number(value):
    return Decimal(value.replace(",", "").replace("，", "").replace("−", "-").replace("－", "-"))


@dataclass(frozen=True)
class SourceAmount:
    source_id: str
    label: str
    raw: str
    unit: str
    value: Decimal
    year: int | None
    column: int = 0
    period_kind: str | None = None


def _period_kind(text):
    # "中期+末期分红" is an annual dividend composition, not H1 reporting.
    if re.search(r"半年|上半年|H1|中报|中期报告", text, re.I):
        return "half"
    if re.search(r"前三季|三季|Q3", text, re.I):
        return "q3"
    if re.search(r"一季|Q1", text, re.I):
        return "q1"
    if re.search(r"全年|年度|年报", text):
        return "annual"
    return None


def _source_period_heading(source):
    # Public PDF page titles may omit the period. Only an explicit report
    # heading at the start of the actual body can fill that omission.
    pattern = r"20\d{2}\s*年?\s*(?:年度|半年度|半年|第[一二三四1234]季度|年报)"
    title = source.get('title', '')
    if re.search(pattern, title):
        return title
    for line in str(source.get('text', '')).splitlines()[:6]:
        if len(line) <= 160 and re.search(pattern, line) and re.search(r'报告|年报|中报|季报', line):
            return line
    return title


def _source_year(source):
    # The disclosure year is NOT the reporting period.
    match = re.search(r"(20\d{2})\s*年?\s*(?:年度|半年度|半年|第[一二三四1234]季度|年报)", _source_period_heading(source))
    return int(match.group(1)) if match else None


def source_amounts(source):
    """Extract up to two comparable currency columns with an explicit unit."""
    sid = source.get("id", "")
    unit = None
    period = _source_year(source)
    period_kind = _period_kind(_source_period_heading(source))
    table_years = []
    comparative = False
    comparative_period_kind=period_kind
    pending = []
    result = []
    for raw_line in str(source.get("text", "")).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.search(r'(?:单位\s*[:：]|^[（(]).*(?:美元|港元|港币|欧元|日元|USD|HKD|EUR|JPY)', line, re.I):
            unit = None
            pending = []
            table_years = []
            comparative = False
            comparative_period_kind=period_kind
            continue
        header = TABLE_UNIT.search(line)
        row_unit=ROW_UNIT.search(line)
        first_number=NUMBERS.search(line)
        inline_row_unit=bool(row_unit and first_number and row_unit.end()<=first_number.start())
        if header and not inline_row_unit:
            unit = header.group(1)
            pending = []
            table_years = []
            comparative = False
            comparative_period_kind=period_kind
            continue
        if re.search(r"单位\s*[:：]", line):
            # A later share/ton/USD table cannot inherit an earlier RMB unit.
            unit = None
            pending = []
            table_years = []
            comparative = False
            comparative_period_kind=period_kind
            continue
        found = list(NUMBERS.finditer(line))
        explicit_years = YEARS.findall(line)
        if explicit_years and len(found) == len(explicit_years) and all(len(m.group()) == 4 for m in found):
            if len(explicit_years) >= 2:
                table_years = [int(y) for y in explicit_years[:2]]
            pending = []
            continue
        if not found:
            # This header contains an actual prior-period column even when a
            # third column says "同比增减". It is not a revenue/cost table.
            if re.search(r'(?:本报告期(?:末)?|本期|期末)\s+(?:上年同期|上年度末|上期|期初)',line):
                comparative=True
                comparative_period_kind='annual' if '上年度末' in line else period_kind
            if re.search(r"上期|上年|期初", line) and not re.search(r"同比|增减|增长|比上", line):
                comparative = True
            if SKIP_LABEL.search(line):
                pending = []
                continue
            pending = (pending + [line])[-3:]
            continue
        prefix = line[:found[0].start()].strip()
        label = " ".join(pending + [prefix]).strip()
        pending = []
        row_unit=ROW_UNIT.search(label)
        amount_unit=row_unit.group(1) if row_unit else unit
        # Row-local parentheses apply only to this labelled row. They must not
        # become a table unit for a later share, percentage or currency row.
        if row_unit: label=ROW_UNIT.sub('',label).strip()
        if re.search(r'[（(][^）)]*(?:美元|港元|港币|欧元|日元|USD|HKD|EUR|JPY)[^）)]*[）)]',label,re.I): continue
        if not label or SKIP_LABEL.search(label) or re.search(r"页|/\s*$", prefix):
            continue
        inline = list(AMOUNT.finditer(line))
        if len(inline) == len(found) == 1:
            for index, match in enumerate(inline):
                amount_unit = match.group(2)
                result.append(SourceAmount(sid, label, match.group(1), amount_unit,
                                           number(match.group(1)) * UNITS[amount_unit], period, index, period_kind))
            continue
        if not amount_unit:
            continue
        # Prose with numbers is not a table row. Explicit inline currency was
        # handled above; mixed prose/percent/year rows are left to source review.
        suffix = line[found[0].start():]
        if re.search(r"[\u4e00-\u9fff]", suffix) or len(prefix) > 45:
            continue
        for index, match in enumerate(found[:2]):
            if index > 0 and len(table_years) <= index and not comparative:
                # The second currency column may be cost instead of a prior
                # period; never assign it the first column's project label.
                continue
            raw = match.group()
            if not re.search(r"[.,，]", raw) and abs(number(raw)) < 10000:
                continue
            year = (table_years[index] if index < len(table_years) else
                    period if index == 0 else period - 1 if period and comparative else None)
            result.append(SourceAmount(sid, label, raw, amount_unit, number(raw) * UNITS[amount_unit], year, index,
                                       period_kind if index==0 else comparative_period_kind))
    return result


def _overlap(label, context):
    label, context = normalized(label), normalized(context)
    # A repurchase row can mention cash dividends as an accounting category;
    # that does not make it the standalone cash-dividend amount.
    if "回购" in label and "回购" not in context:
        return False
    if "合计" in label and not any(word in context for word in ("合计", "总计", "总额", "共计")):
        return False
    if ("中期" in label and "末期" in context) or ("末期" in label and "中期" in context):
        return False
    words = {context[i:i + 2] for i in range(max(0, len(context) - 1))}
    return any(w in label for w in words if w not in WEAK and not w.isdigit())


def _precision(raw):
    return Decimal(1).scaleb(-len(raw.split(".")[1])) if "." in raw else Decimal(1)


def _matches(expected, shown, precision):
    return expected.quantize(precision, rounding=ROUND_HALF_UP) == shown


def check_text(text, source_values, *, field="body"):
    findings = []
    for match in AMOUNT.finditer(text):
        raw, unit = match.groups()
        shown = number(raw)
        if shown == 0:
            continue
        # The nearest clause separates an actual amount from a forecast later
        # in the same paragraph and a total from an earlier per-share dividend.
        prefix = text[:match.start()]
        clause = re.split(r"[。；;，,\n、]", prefix)[-1]
        if SKIP_LABEL.search(clause) or NON_FACT.search(clause):
            continue
        years = list(YEARS.finditer(prefix))
        year = int(years[-1].group(1)) if years else None
        period_prefix = prefix[years[-1].start():] if years else re.split(r"[。；;\n]", prefix)[-1]
        period_kind = _period_kind(period_prefix)
        candidates = [v for v in source_values if v.value * shown > 0 and
                      (year is None or year == v.year) and
                      (period_kind is None or period_kind == v.period_kind)]
        # A named project must agree. Generic totals can be matched only when
        # there is a unique powers-of-ten conversion in the cited currency rows.
        named = [v for v in candidates if _overlap(v.label, clause)]
        generic = bool(re.fullmatch(r"\s*(?:约|合计|共计|总计|为|约为|合计约|合计为|达到|金额为|金额约)*\s*", clause))
        if named:
            candidates = named
        elif not generic:
            continue
        precision = _precision(raw.replace(",", "").replace("，", ""))
        if any(_matches(v.value / UNITS[unit], shown, precision) for v in candidates):
            continue
        wrong = []
        for value in candidates:
            expected = value.value / UNITS[unit]
            for exponent in (-4, -3, -2, -1, 1, 2, 3, 4):
                if _matches(expected / (Decimal(10) ** exponent), shown, precision):
                    wrong.append((value, expected, exponent))
                    break
        amounts = {v.value for v, _, _ in wrong}
        if len(amounts) != 1:
            continue
        value, expected, exponent = wrong[0]
        findings.append({"field": field, "claim": match.group(), "source_id": value.source_id,
                         "source_label": value.label, "source_amount": value.raw + value.unit,
                         "expected": str(expected.quantize(precision, rounding=ROUND_HALF_UP)) + unit,
                         "factor": str(Decimal(10) ** abs(exponent))})
    return findings


def memo_findings(candidate, run):
    sources = {s.get("id"): s for s in run.get("research_sources", []) if s.get("id")}
    parsed = {sid: source_amounts(source) for sid, source in sources.items()}
    all_ids = set(sid for item in candidate.get("theses", []) + (candidate.get("sections", []) if isinstance(candidate.get("sections"), list) else [])
                  if isinstance(item, dict) for sid in item.get("evidence_ids", []))
    findings = []
    def inspect(text, ids, field):
        if isinstance(text, str):
            findings.extend(check_text(text, [v for sid in ids for v in parsed.get(sid, [])], field=field))
    inspect(candidate.get("summary"), all_ids, "summary")
    for key in ("theses", "sections"):
        items = candidate.get(key, [])
        if isinstance(items, dict):
            items = list(items.values())
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for prop in ("body", "counter_evidence", "invalidate_if"):
                inspect(item.get(prop), item.get("evidence_ids", []), f"{key}[{index}].{prop}")
    for index, answer in enumerate(candidate.get("question_answers", [])):
        if isinstance(answer, dict):
            inspect(answer.get("answer"), [b.get("source_id") for b in answer.get("bindings", []) if isinstance(b, dict)],
                    f"question_answers[{index}].answer")
    return findings


def assert_memo_amounts(candidate, run):
    findings = memo_findings(candidate, run)
    if findings:
        details = [f"{f['field']} 的 {f['claim']} 与引用 {f['source_id']} 的“{f['source_label']} {f['source_amount']}”不符，"
                   f"换算应约为 {f['expected']}（差 {f['factor']} 倍）" for f in findings[:10]]
        raise ValueError("金额单位换算校验未通过：" + "；".join(details) +
                         "。请逐项按原文单位用元÷100000000、千元÷100000、万元÷10000、百万元÷100换算亿元，同步修改正文及问题答案，不得删除引用绕过校验；预测与派生计算请明确标注假设和公式。")
