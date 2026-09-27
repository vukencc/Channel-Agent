"""Generate an auditable Markdown report and every-query metric comparison."""
import csv
import gzip
import json
import html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ARCHIVE = Path(__file__).resolve().parent
REPORT = ROOT / '.cache/reports/rag/full-corpus'


def load_records(name):
    with gzip.open(REPORT / name, 'rt', encoding='utf-8') as f:
        return [json.loads(line) for line in f]


def main():
    summary = json.loads((REPORT / 'evaluation-summary.json').read_text())
    baseline = json.loads((REPORT / 'baseline-summary.json').read_text())
    manifest = json.loads((ARCHIVE / 'inputs/queries.json').read_text())
    calibration = json.loads((REPORT / 'calibration.json').read_text())
    records = load_records('evaluation-traces.jsonl.gz')
    baseline_records = {row['qid']: row for row in load_records('baseline-traces.jsonl.gz')}
    comparisons = []
    for row in records:
        item = {'qid': row['qid'], 'query': row['query']}
        for stage, values in {'baseline': baseline_records[row['qid']]['metrics'], **row['metrics']}.items():
            for metric, value in values.items():
                item[f'{stage}_{metric}'] = value
        item['rerank_minus_vector_ndcg@10'] = item['rerank_ndcg@10'] - item['vector_ndcg@10']
        item['rerank_minus_rrf_ndcg@10'] = item['rerank_ndcg@10'] - item['rrf_ndcg@10']
        comparisons.append(item)
    with (REPORT / 'per-query.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(comparisons[0]))
        writer.writeheader()
        writer.writerows(comparisons)
    lines = [
        '# RAG 完整链路评测报告', '',
        f'完整官方语料 **{manifest["corpus_count"]:,} 篇**；固定 50 条校准、100 条评测查询。',
        '数据与模型版本见 [assets.json](assets.json)，查询及选择规则见 [queries.json](queries.json)。',
        '这是固定子集评测，不是 C-MTEB 全量榜单成绩。查询、标注和语料均未按检索效果筛选。', '',
        '## 实现', '',
        '向量子块召回按父段落最大余弦分数汇总；BM25 独立检索父段落；两路各取 50 条。',
        'RRF 使用等权、k=60；CrossEncoder 对融合前 50 条推理，长段落分 token 窗口并取最高 logit。',
        'strictness 只过滤重排后的输出，不影响前面的候选。分数不是概率，各阶段分数不可直接相加。',
        '索引复用与内容寻址向量缓存避免重复编码；磁盘映射和事务支持中断后续跑。', '',
        '## 质量对比', '',
        '指标先将父段落排名按原始文档 ID 去重，再计算 @10；使用官方正例标注，未标注文档按不相关计。',
        '向量/BM25/RRF/重排使用过滤前真实排名；filtered 单列实际最终输出。', '',
        '| 阶段 | Recall@10 | MRR@10 | nDCG@10 |',
        '|---|---:|---:|---:|',
    ]
    for stage, values in {'旧检索逻辑（同 INT8 编码器）': baseline['metrics'], **summary['metrics']}.items():
        lines.append(f'| {stage} | {values["recall@10"]:.4f} | {values["mrr@10"]:.4f} | {values["ndcg@10"]:.4f} |')
    lines.extend(['', '每条查询的对比见 [per-query.csv](per-query.csv)；完整文本、分数、排名及最终工具输出在',
                  '[evaluation-traces.jsonl.gz](evaluation-traces.jsonl.gz)。', ''])
    for key, label in [('rerank_minus_vector_ndcg@10', '重排相对向量'), ('rerank_minus_rrf_ndcg@10', '重排相对 RRF')]:
        values = [row[key] for row in comparisons]
        lines.append(f'{label}：提升 {sum(v > 1e-9 for v in values)} 条，退步 {sum(v < -1e-9 for v in values)} 条，持平 {sum(abs(v) <= 1e-9 for v in values)} 条。')
    lines.extend(['', '## 阶段耗时', '',
                  f'本次索引构建/缓存加载共 {summary["index_build_seconds"]:.2f} 秒；'
                  f'{summary["parents"]:,} 个父段落、{summary["children"]:,} 个子块。', '',
                  '| 阶段 | 平均秒 | P95 秒 |', '|---|---:|---:|'])
    for stage, times in summary['timings'].items():
        lines.append(f'| {stage} | {times["mean"]:.4f} | {times["p95"]:.4f} |')
    lines.extend(['', '## 真实结果示例', '',
                  '以下先展示固定评测清单的前 3 条，再展示重排相对 RRF 差值最小的案例（可能退步或持平）；不是按成功与否挑选。'])
    worst = min(comparisons, key=lambda row: row['rerank_minus_rrf_ndcg@10'])['qid']
    selected = records[:3] + [row for row in records if row['qid'] == worst and row not in records[:3]]
    for row in selected:
        lines.extend(['', f'### 查询 {row["qid"]}：{row["query"]}', '', '| 阶段 | Top-3 原始文档 ID（分数） |', '|---|---|'])
        for stage, hits in row['stages'].items():
            seen, examples = set(), []
            for hit in hits:
                identifier = hit['metadata']['doc_id']
                if identifier in seen:
                    continue
                seen.add(identifier)
                examples.append(f'{identifier} ({hit["score"]:.4f})')
                if len(examples) == 3:
                    break
            lines.append(f'| {stage} | {", ".join(examples)} |')
        lines.append('')
        for stage, hits in row['stages'].items():
            if hits:
                excerpt = html.escape(hits[0]['document'][:100]).replace('\n', ' ')
                lines.append(f'- **{stage} 第一名原文片段**：{excerpt}')
    lines.extend(['', '## 输出阈值与局限', '',
                  f'仅用 50 条校准查询得到 strict={calibration["strict"]:.4f}、normal={calibration["normal"]:.4f}、loose={calibration["loose"]:.4f}。',
                  '阈值优化目标分别为 F0.5/F1/F2，严格顺序采用最小 0.1 间距。这些值仅适用于本模型与此次公开基准；不保证跨语料拒答效果。',
                  '官方标注可能不完整；预训练模型的数据重叠未做独立审计。100 条固定查询不足以证明普遍提升。',
                  '旧检索逻辑基线与新流水线使用同一 INT8 编码器，并非原 FP32 模型逐位重放。基线保留分块、无前缀查询、0.45 阈值和父段落去重，仅缓存构建以便运行完整语料。新向量阶段还改变了长度上限和查询指令，基线到最终的差异包含这些因素。',
                  'BM25 父段落召回与向量子块召回粒度不同，但融合 ID 一致。重排只能重新排列候选，无法找回两路都漏掉的文档。',
                  '完整协议及复现说明见 [PROTOCOL.md](PROTOCOL.md) 和 [开发说明](../../docs/rag.md)。', ''])
    (REPORT / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
