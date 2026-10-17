"""Standalone report styles: no fonts, scripts, or network assets required."""

REPORT_CSS = r'''
:root { color-scheme:light; --purple:#8d2466; --purple-dark:#65204d; --ink:#282631; --muted:#716b77; --line:#e6dfe6; --wash:#faf5f9; }
* { box-sizing:border-box; }
html { background:#f2eff2; }
body { margin:0; color:var(--ink); font:15px/1.85 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }
.paper { max-width:980px; margin:36px auto; padding:48px 64px; background:white; box-shadow:0 14px 55px #3423350c; }
.report-header { border-top:6px solid var(--purple); padding:30px 0 28px; border-bottom:1px solid var(--line); }
.brand { color:var(--purple); font-size:12px; font-weight:700; letter-spacing:2px; }
.company-line { margin:22px 0 6px; font-size:17px; font-weight:600; }
.company-line span { color:var(--muted); font-size:14px; font-weight:400; margin-left:14px; }
h1 { font-size:31px; line-height:1.5; letter-spacing:-.5px; margin:8px 0 20px; font-weight:650; }
.report-meta { display:flex; flex-wrap:wrap; gap:12px 30px; color:var(--muted); font-size:13px; }
.report-meta strong { color:var(--ink); font-weight:500; margin-left:8px; }
.research-question { margin:20px 0 0; color:var(--muted); }
.research-question strong { color:var(--ink); }
h2 { font-size:22px; line-height:1.5; margin:38px 0 18px; padding-bottom:10px; border-bottom:1px solid var(--line); color:var(--purple-dark); }
h3 { margin:22px 0 10px; font-size:16px; line-height:1.6; }
h4,h5,h6 { font-size:15px; }
p { margin:10px 0; }
a { color:var(--purple); text-decoration:none; border-bottom:1px solid #d8b8cb; overflow-wrap:anywhere; }
a:hover { color:var(--purple-dark); }
.muted,.source-meta { color:var(--muted); font-size:13px; }
.recommendation { margin:28px 0 36px; padding:24px 28px; background:var(--wash); border:1px solid #e9d4e2; border-left:5px solid var(--purple); }
.recommendation h2 { margin:0 0 16px; padding:0; border:0; font-size:24px; }
.recommendation h3 { font-size:15px; }
.recommendation-positions { display:grid; grid-template-columns:1fr 1fr; gap:22px; margin:20px 0; padding:14px 0; border-top:1px solid #e9d4e2; border-bottom:1px solid #e9d4e2; }
.recommendation-positions p { margin:0; }
.recommendation-positions strong { display:block; color:var(--purple-dark); margin-bottom:4px; }
.recommendation-conditions { display:grid; grid-template-columns:1fr 1fr; gap:0 26px; }
.recommendation-conditions section { break-inside:avoid; }
.recommendation-pricing { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:22px 0; }
.price-tile { display:flex; flex-direction:column; align-items:flex-start; gap:6px; padding:16px 18px; background:#f3ecf2; border:1px solid #e7dae3; border-radius:5px; font-size:13px; color:var(--muted); }
.price-tile>strong { font-size:26px; line-height:1.5; font-weight:600; color:var(--purple-dark); font-variant-numeric:tabular-nums; }
.price-tile small { font-size:12px; font-weight:500; }
.recommendation-frozen-notice,.basis-notice { padding:14px 18px; background:#fff8ed; border-left:3px solid #b88238; color:#765526; }
.basis-notice h3 { margin-top:0; }
.summary { margin:18px 0; padding:16px 20px; background:var(--wash); border-left:3px solid #d5a5c2; }
.counter { padding:14px 20px; background:#f8f7f9; border-left:3px solid #c5bdca; }
.counter p:last-child { margin-bottom:0; }
.thesis { margin:24px 0; }
table { width:100%; border-collapse:collapse; margin:18px 0 24px; font-size:13px; font-variant-numeric:tabular-nums; }
th,td { padding:11px 12px; vertical-align:top; text-align:left; border-bottom:1px solid var(--line); overflow-wrap:anywhere; }
thead { background:#f6f0f5; color:var(--purple-dark); }
thead th { font-weight:600; }
.financial-table th:not(:first-child),.financial-table td { text-align:right; }
.financial-table td:last-child { text-align:left; }
tbody tr:nth-child(even) { background:#fcfafc; }
.extension-table { table-layout:fixed; }
ul,ol { padding-left:22px; }
li { margin:6px 0; }
blockquote { margin:18px 0; padding:10px 20px; border-left:3px solid #d8c1d1; color:#625768; background:#fcfafc; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.7 ui-monospace,Consolas,monospace; background:#f7f5f8; padding:15px 18px; border:1px solid var(--line); }
details { margin:18px 0; border-bottom:1px solid var(--line); padding-bottom:12px; }
summary { cursor:pointer; font-size:14px; font-weight:600; color:var(--purple-dark); }
.appendix { margin-top:42px; padding-top:8px; border-top:2px solid var(--purple); }
.source-entry { padding:0 0 12px; margin-bottom:18px; border-bottom:1px solid var(--line); }
.source-entry h3 { margin:12px 0 4px; }
footer { border-top:1px solid var(--line); margin-top:36px; padding-top:16px; }
.print-hint { font-size:12px; color:var(--muted); text-align:right; margin:14px 0 0; }
@media (max-width:720px) {
 .paper { margin:0; padding:24px 20px; box-shadow:none; }
 h1 { font-size:26px; } h2 { font-size:20px; }
 .recommendation { padding:20px; }
 .recommendation-positions { grid-template-columns:1fr; gap:14px; }
 .recommendation-conditions { grid-template-columns:1fr; }
 .recommendation-pricing { grid-template-columns:1fr; }
 .report-meta { gap:8px 18px; }
 th,td { padding:8px 5px; }
}
@page { size:A4; margin:17mm 16mm 19mm; }
@media print {
 html,body { background:white; }
 body { font-size:10.5pt; line-height:1.7; print-color-adjust:exact; -webkit-print-color-adjust:exact; }
 .paper { margin:0; padding:0; width:auto; max-width:none; box-shadow:none; }
 .report-header { padding-top:20px; }
 h1 { font-size:23pt; } h2 { font-size:15pt; margin-top:24px; } h3 { font-size:11pt; }
 .report-meta,.muted,.source-meta { font-size:9pt; }
 h1,h2,h3,h4,summary { break-after:avoid; }
 .source-intro,.source-meta { break-after:avoid; }
 p { orphans:3; widows:3; }
 thead { display:table-header-group; }
 tr,.recommendation-positions,.counter,.basis-notice { break-inside:avoid; }
 table { font-size:9pt; } th,td { padding:8px 7px; }
 .recommendation { padding:18px 20px; margin:22px 0; }
 .recommendation h2 { font-size:17pt; }
 .recommendation-positions { display:grid; grid-template-columns:1fr 1fr; }
 .recommendation-conditions { grid-template-columns:1fr 1fr; }
 .recommendation-pricing { grid-template-columns:1fr 1fr; break-inside:avoid; }
 details { display:block; }
 details > *:not(summary) { display:block !important; }
 details::details-content { display:block; content-visibility:visible; }
 .print-hint { display:none; }
 pre { font-size:8pt; }
}
'''
