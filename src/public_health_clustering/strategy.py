"""Evidence-grounded Chinese cluster strategy briefs without run persistence."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .knowledge_base import document_json, search_knowledge
from .reporting import ClusterReport, ClusterReportError, build_cluster_reports


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyContext:
    report: ClusterReport
    noise: dict
    sources: list[dict]


def build_strategy_context(session: Session, run_id: int, cluster_id: int) -> StrategyContext:
    run, reports, noise = build_cluster_reports(session, run_id)
    del run
    report = next((item for item in reports if item.cluster_id == cluster_id), None)
    if report is None:
        raise StrategyError(f"运行 {run_id} 中不存在聚类 {cluster_id}")
    sources = [document_json(item) for item in search_knowledge(session, disease=report.illness)]
    if not sources:
        raise StrategyError(f"知识库中没有疾病“{report.illness}”的现行资料")
    return StrategyContext(report=report, noise=noise, sources=sources)


def generate_strategy_brief(context: StrategyContext) -> dict:
    """Produce a reproducible baseline brief from aggregate facts and cited sections."""
    report, sources = context.report, context.sources
    evidence = []
    for document in sources:
        for index, section in enumerate(document["sections"], 1):
            evidence.append({
                "evidence_id": f"文献{document['document_id']}-{index}",
                "disease": document["disease"], "title": document["title"],
                "section": section["title"], "content": section["content"],
                "source_url": document["source_url"], "version": document["version"],
            })
    citation_ids = [item["evidence_id"] for item in evidence]
    first = citation_ids[:1]
    second = citation_ids[1:2] or first
    third = citation_ids[2:3] or first
    activity = "病例日期跨度较短，建议优先核查是否仍在持续增加" if report.active_days <= 3 else "病例分布跨越多个日期，建议持续监测趋势变化"
    return {
        "mode": "规则化证据简报",
        "title": f"{report.illness}聚类 {report.cluster_id} 防控策略简报",
        "assessment": (
            f"该聚类包含 {report.case_count} 例病例，诊断日期为 {report.first_diagnosis_date} 至 "
            f"{report.last_diagnosis_date}，高峰日 {report.peak_diagnosis_date} 报告 {report.peak_daily_cases} 例；"
            f"代理位置占比 {report.proxy_share:.1%}。{activity}。"
        ),
        "priorities": [
            {"priority": "立即", "action": "核对病例定义、诊断日期、重复记录和报告完整性，并结合接触史确认聚集事件范围。", "citations": first},
            {"priority": "24小时内", "action": "按现行规范开展流行病学调查、重点人群和重点场所排查，并落实相应监测与报告措施。", "citations": second},
            {"priority": "持续", "action": "每日更新病例曲线、人口学构成和空间分布，记录措施执行情况，并根据新增证据调整响应范围。", "citations": third},
        ],
        "data_quality": [
            f"年龄完整度：{report.age_known_count}/{report.case_count}；性别完整度：{report.sex_known_count}/{report.case_count}。",
            f"代理位置：{report.proxy_case_count} 例；空间覆盖面积仅为分析足迹，不代表传播边界。",
            "当前资料没有人口分母，不能据病例构成推断人群罹患率。",
        ],
        "escalation": "若病例继续增加、出现重症或死亡、涉及重点机构或跨区域传播迹象，应立即按当地现行预案升级研判和报告。",
        "evidence": evidence,
        "disclaimer": "本简报用于辅助公共卫生研判，不替代法定报告要求、官方原文、临床判断或属地处置决定。",
    }
