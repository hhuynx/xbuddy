"""Self-contained local report exports; no raw credentials or external assets."""
from __future__ import annotations

import csv
import html
import io
import math
import re

from server.services import ServiceError
from server.storage.database import encode, fingerprint
from server.ui.presentation import LABELS, claim_text, financial_rows, price_charts, reason, stamp
from server.publication import PARAGRAPH_LABELS


def md(value):
    text = str(value).replace('\r', '').replace('\n', ' ')
    return re.sub(r'([\\`*_{}\[\]()#+!|<>])', r'\\\1', text)


def csv_cell(value):
    if value is None:
        return ''
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t')):
        return "'" + value
    return value


def evidence_index(service, owner, artifact):
    report = artifact['content']
    sources = []
    for source in report['sources']:
        record = service.resource('evidence', source['evidence_id'], owner)
        raw = service.resource('raw_records', record['raw_id'], owner)
        c = record['content']
        sources.append({'id': record['id'], 'run_id': record['run_id'], 'quality': c['quality'],
            'provider': c['provider'], 'tool': c['tool'], 'request_id': c['request_id'], 'as_of': c['as_of'],
            'fetched_at': c['fetched_at'], 'units': c['units'], 'method': c['method'], 'parameters': c['parameters'],
            'field_map': c['field_map'], 'evidence_sha256': record['content_sha256'], 'raw_sha256': raw['content_sha256'],
            'record_count': len(c['records'])})
    return {'schema_version': 1, 'artifact_id': artifact['id'], 'run_id': artifact['run_id'], 'report_version': artifact['version'],
        'report_sha256': fingerprint(report), 'validation_status': report['validation_status'],
        'sources': sources, 'claims': [{k: c.get(k) for k in ('kind','field','symbol','unit','as_of','reference','evidence_ids','derived_metric_ids','limitations')} for c in report['claims']],
        'calculations': report['computations'], 'knowledge_references': report.get('knowledge_context', {}),
        'data_policy': 'source metadata and field references; raw market responses excluded'}


def markdown(artifact):
    r = artifact['content']
    lines = ['# XBuddy 研究报告', '', f"版本：{artifact['version']} · 运行：{artifact['run_id']}",
        f"生成时间：{stamp(r['created_at'])}（上海时间）", f"状态：{md(r['status'])} · 校验：{md(r['validation_status'])}", '',
        '## 研究目标', '', md(r['goal']), '', '## 研究结论', '']
    for section in r.get('narrative',[]):
        lines += ['### '+md(section['title']), '']
        for paragraph in section['paragraphs']:
            lines += ['**'+PARAGRAPH_LABELS[paragraph['kind']]+'**', '', md(paragraph['text']), '', '依据：'+md(', '.join(paragraph.get('fact_refs',[]))) if paragraph.get('fact_refs') else '本段未引用核验事实。', '']
    for kind, label in (('fact','事实'), ('inference','推断'), ('uncertain','不确定信息')):
        claims = [c for c in r['claims'] if c['kind'] == kind]
        if claims:
            lines += ['### '+label, '']
        for c in claims:
            lines += ['- ' + md(claim_text(c)), '  - 数据时点：'+md(stamp(c.get('as_of'))) if c.get('as_of') else '  - 解释边界：'+md('；'.join(c.get('limitations', []))),
                '  - 证据：'+md(', '.join(c.get('evidence_ids', [])))+'；计算：'+md(', '.join(c.get('derived_metric_ids', []))),
                '  - 精确字段：'+md(encode(c.get('reference', {})))]
        if claims:
            lines += ['']
    rows = financial_rows(r)
    if rows:
        lines += ['## 已披露财务数据', '', '| 标的 | 报告期 | 营业收入 | 归母净利润 | 每股收益 |', '| --- | --- | ---: | ---: | ---: |']
        lines += ['| ' + ' | '.join(md(row[k]) for k in ('symbol','period','operating_income','parent_holder_net_profit','basic_eps')) + ' |' for row in rows]
        lines += ['']
    lines += ['## 计算口径', '']
    for m in r['computations']:
        lines += ['- 计算 '+md(m['metric_id'])+'：'+md(encode(m['content']['method']))]
    lines += ['', '## 数据缺口与研究边界', '']
    lines += ['- '+md(reason(g['reason']) + ('：'+g['detail'] if g.get('detail') else '')) for g in r['gaps']]
    lines += ['- '+md(text) for text in r['limitations']]
    if r.get('execution_issues'):
        lines += ['', '## 调用与数据质量记录', '']
        lines += ['- '+md(reason(g['reason'])+('：'+g['detail'] if g.get('detail') else '')) for g in r['execution_issues']]
    if r.get('execution_failures'):
        lines += ['', '## 执行问题', '']
        lines += ['- '+md(reason(e['reason'])+'：'+e['detail']) for e in r['execution_failures']]
    if r['validation_status'] == 'failed':
        lines += ['- 核心结论未通过校验：'+md(', '.join(r['validation_errors']))]
    for note in r.get('knowledge_context', {}).get('user_notes', []):
        lines += ['- 未验证的'+md(note['label'])+'：'+md(note['text'])]
    lines += ['', '## 来源与时点', '']
    for s in r['sources']:
        lines += ['- '+md(encode(s))]
    lines += ['', '## 产物追溯', '', '前一版本：'+md(r.get('previous_artifact_id') or '无'),
        '关联研究：'+md(encode(r.get('knowledge_context', {}).get('followup'))),
        '记忆引用：'+md(encode(r.get('knowledge_context', {}).get('memory_usage', []))),
        '压缩记录：'+md(encode(r.get('knowledge_context', {}).get('context_snapshots', []))),
        '', '历史观察与财务差异仅描述已有样本，未估计未来表现。', '']
    return '\n'.join(lines)


def claims_csv(artifact):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['report_version','run_id','kind','symbol','field','value','unit','as_of','text','evidence_ids','metric_ids','reference','method','limitations'])
    for c in artifact['content']['claims']:
        writer.writerow([csv_cell(v) for v in [artifact['version'], artifact['run_id'], c['kind'], c.get('symbol',''), c.get('field',''), c.get('value'), c.get('unit',''),
            c.get('as_of',''), c['text'], encode(c.get('evidence_ids', [])), encode(c.get('derived_metric_ids', [])), encode(c.get('reference', {})),
            encode(c.get('method', {})), encode(c.get('limitations', []))]])
    return '\ufeff' + output.getvalue()


def svg_chart(chart):
    options = chart['options']
    values = [v for s in options['series'] for v in s['data'] if type(v) in (int,float) and math.isfinite(v)]
    if not values:
        return ''
    low, high = min(values), max(values)
    span = high-low or max(1, abs(high)*.05)
    points = max(1, len(options['xAxis']['data'])-1)
    pieces = ['<svg viewBox="0 0 800 260" role="img" aria-label="已获取历史收盘价格走势">']
    for tick in range(5):
        y = 20 + tick*45
        val = high-span*tick/4
        pieces.append(f'<line x1="64" x2="780" y1="{y}" y2="{y}" stroke="#dde5e7"/><text x="6" y="{y+5}" font-size="12">{val:.2f}</text>')
    for i, series in enumerate(options['series']):
        segments, segment = [], []
        for index, value in enumerate(series['data']):
            if value is None:
                if segment:
                    segments.append(segment)
                segment = []
                continue
            segment.append(f'{64+index/points*716:.2f},{20+(high-value)/span*180:.2f}')
        if segment:
            segments.append(segment)
        color = options['color'][i % len(options['color'])]
        pieces += [f'<polyline points="{" ".join(s)}" fill="none" stroke="{color}" stroke-width="2"/>' for s in segments]
        pieces.append(f'<text x="{64+i*230}" y="252" fill="{color}" font-size="13">{html.escape(series["name"])}</text>')
    dates = options['xAxis']['data']
    if dates:
        pieces += [f'<text x="64" y="222" font-size="12">{html.escape(dates[0])}</text>', f'<text x="680" y="222" font-size="12">{html.escape(dates[-1])}</text>']
    return ''.join(pieces)+'</svg>'


def html_report(service, owner, artifact, print_view=False):
    r = artifact['content']
    esc = lambda v: html.escape(str(v), quote=True)
    body = ['<h1>XBuddy 研究报告</h1>', f'<p class="meta">版本 {artifact["version"]} · {esc(stamp(r["created_at"]))}（上海时间） · {esc(LABELS.get(r["status"],r["status"]))} / {esc(LABELS.get(r["validation_status"],r["validation_status"]))}</p>',
        '<h2>研究目标</h2>', '<p>'+esc(r['goal'])+'</p>']
    if r['validation_status'] != 'passed':
        body += ['<p class="warning">核心结论未通过校验。</p>']
    else:
        charts, _ = price_charts(service, owner, artifact['run_id'], service.collection(owner, artifact['run_id'], 'evidence'))
        for chart in charts:
            body += ['<h2>历史收盘价格</h2>', '<p class="meta">'+esc({'forward':'前复权','backward':'后复权','none':'未复权'}.get(chart['adjust'],chart['adjust']))+' · 日线 · 元/股</p>', svg_chart(chart)]
        rows = financial_rows(r)
        if rows:
            body += ['<h2>已披露财务数据</h2><table><thead><tr><th>标的</th><th>报告期</th><th>营业收入</th><th>归母净利润</th><th>每股收益</th></tr></thead><tbody>']
            for row in rows:
                body += ['<tr>'+''.join('<td>'+esc(row[k])+'</td>' for k in ('symbol','period','operating_income','parent_holder_net_profit','basic_eps'))+'</tr>']
            body += ['</tbody></table>']
    for section in r.get('narrative',[]):
        body += ['<h2>'+esc(section['title'])+'</h2>']
        for paragraph in section['paragraphs']:
            body += ['<article><p class="meta">'+PARAGRAPH_LABELS[paragraph['kind']]+'</p><p>'+esc(paragraph['text']).replace('\n','<br>')+'</p><p class="meta">'+esc('、'.join(paragraph.get('fact_refs',[])))+'</p></article>']
    body += ['<h2>研究结论与依据</h2>']
    for c in r['claims']:
        label = {'fact':'事实','inference':'推断','uncertain':'不确定信息'}[c['kind']]
        body += ['<article><p class="meta">'+label+'</p><p>'+esc(claim_text(c))+'</p>',
            '<p class="meta">数据时点：'+esc(stamp(c['as_of']))+' · 原始单位：'+esc(c.get('unit') or '无')+'</p>' if c.get('as_of') else '',
            '<p class="meta">证据：'+esc('、'.join(c.get('evidence_ids',[])) or '无')+' · 计算：'+esc('、'.join(c.get('derived_metric_ids',[])) or '无')+'</p>',
            '<details class="technical-details"><summary>精确引用与计算口径</summary><pre>'+esc(encode({k: c.get(k) for k in ('as_of','unit','reference','evidence_ids','derived_metric_ids','method','limitations')}))+'</pre></details></article>']
    body += ['<h2>数据缺口与研究边界</h2><ul>']
    body += ['<li>'+esc(reason(g['reason'])+('：'+g['detail'] if g.get('detail') else ''))+'</li>' for g in r['gaps']]
    body += ['<li>'+esc(x)+'</li>' for x in r['limitations']]
    body += ['</ul>']
    if r.get('execution_issues'):
        body += ['<h2>调用与数据质量记录</h2><ul>']
        body += ['<li>'+esc(reason(g['reason'])+('：'+g['detail'] if g.get('detail') else ''))+'</li>' for g in r['execution_issues']]
        body += ['</ul>']
    if r.get('execution_failures'):
        body += ['<h2>执行问题</h2><ul>']
        body += ['<li>'+esc(reason(e['reason'])+'：'+e['detail'])+'</li>' for e in r['execution_failures']]
        body += ['</ul>']
    if r.get('knowledge_context', {}).get('user_notes'):
        body += ['<h2>未验证的研究记录</h2>']
    body += ['<p>'+esc(n['label'])+'：'+esc(n['text'])+'</p>' for n in r.get('knowledge_context', {}).get('user_notes', [])]
    body += ['<h2>来源、计算与产物追溯</h2>', '<p class="meta">运行：'+esc(artifact['run_id'])+' · 产物：'+esc(artifact['id'])+'</p>']
    for source in r['sources']:
        body += ['<p class="meta">证据 '+esc(source['evidence_id'])+' · '+esc(source['provider'])+'<br>数据时点：'+esc(stamp(source['as_of']))+' · 获取时间：'+esc(stamp(source['fetched_at']))+'</p>']
    for metric in r['computations']:
        body += ['<p class="meta">计算 '+esc(metric['metric_id'])+'</p><pre>'+esc(encode(metric['content']['method']))+'</pre>']
    body += ['<p class="meta">精确字段映射、原文指纹与版本引用保留在下方索引，也可在工作台单独导出证据索引 JSON。</p>',
        '<details class="technical-details"><summary>完整证据索引</summary><pre>'+esc(encode(evidence_index(service, owner, artifact)))+'</pre></details>']
    button = '<button class="print" onclick="window.print()">打印 / 保存为 PDF</button>' if print_view else ''
    css = 'body{font-family:"Microsoft YaHei",sans-serif;color:#172d3a;max-width:960px;margin:32px auto;padding:0 24px;line-height:1.7}h1,h2{color:#0f766e}h2{margin-top:28px}.meta{color:#617782;font-size:12px;overflow-wrap:anywhere}article{border-top:1px solid #dde5e7;padding:12px 0}article p{margin:5px 0}summary{cursor:pointer;color:#0f766e;font-size:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;background:#f5f8f7;padding:12px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border-bottom:1px solid #dde5e7;padding:8px;text-align:left}svg{width:100%;height:auto}button{padding:10px 18px;background:#0f766e;color:white;border:0;border-radius:6px}.warning{background:#fff3df;padding:14px}@media print{body{margin:0;padding:0;font-size:11px}.meta{font-size:9px}h2,article,table,svg{break-inside:avoid}.print,.technical-details{display:none}pre{font-size:8px}@page{size:A4;margin:16mm}}'
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>XBuddy 研究报告</title><style>'+css+'</style></head><body>'+button+''.join(body)+'</body></html>'


def export_report(service, owner, artifact_id, format):
    artifact = service.artifacts.verify(owner, artifact_id)
    name = f"xbuddy-{artifact['run_id'][:8]}-v{artifact['version']}"
    if format == 'markdown':
        return markdown(artifact).encode('utf-8'), 'text/markdown; charset=utf-8', name+'.md'
    if format == 'html':
        return html_report(service, owner, artifact).encode('utf-8'), 'text/html; charset=utf-8', name+'.html'
    if format == 'csv':
        return claims_csv(artifact).encode('utf-8'), 'text/csv; charset=utf-8', name+'.csv'
    if format == 'evidence':
        return encode(evidence_index(service, owner, artifact)).encode('utf-8'), 'application/json', name+'-evidence.json'
    raise ServiceError(422, '导出格式无效')
