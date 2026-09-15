"""Model-driven, evidence-grounded Chinese public-health strategy agent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from .knowledge_base import document_json, search_knowledge
from .models import ClusteringRun, ClusteringRunStatus
from .reporting import ClusterReport, build_cluster_reports


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyContext:
    report: ClusterReport
    run: dict
    calendar: dict
    location: dict
    previous_comparison: dict | None
    sources: list[dict]


CITY_CENTERS = {
    "广州": (23.1291, 113.2644), "深圳": (22.5431, 114.0579),
    "上海": (31.2304, 121.4737), "北京": (39.9042, 116.4074),
    "成都": (30.5728, 104.0668), "武汉": (30.5928, 114.3055),
}


def _season(month: int) -> str:
    return "春季" if month in (3, 4, 5) else "夏季" if month in (6, 7, 8) else "秋季" if month in (9, 10, 11) else "冬季"


def _nearest_city(report: ClusterReport) -> dict:
    city, center = min(CITY_CENTERS.items(), key=lambda item: (report.centroid_latitude-item[1][0])**2 + (report.centroid_longitude-item[1][1])**2)
    distance_degrees = ((report.centroid_latitude-center[0])**2 + (report.centroid_longitude-center[1])**2) ** .5
    return {"nearest_configured_city": city if distance_degrees < 1.5 else "未识别", "centroid_precision": "仅提供城市级近似，不提供精确坐标"}


def _previous_cluster(session: Session, run: ClusteringRun, report: ClusterReport) -> dict | None:
    prior_runs = session.scalars(select(ClusteringRun).where(
        ClusteringRun.status == ClusteringRunStatus.COMPLETED,
        ClusteringRun.run_id != run.run_id,
        ClusteringRun.window_end <= run.window_start,
    ).order_by(ClusteringRun.window_end.desc()).limit(5)).all()
    for prior in prior_runs:
        _, reports, _ = build_cluster_reports(session, prior.run_id)
        candidates = [item for item in reports if item.illness == report.illness]
        if candidates:
            closest = min(candidates, key=lambda item: (item.centroid_latitude-report.centroid_latitude)**2 + (item.centroid_longitude-report.centroid_longitude)**2)
            return {"run_id": prior.run_id, "window_end": prior.window_end.date().isoformat(), "case_count": closest.case_count,
                    "case_count_change": report.case_count - closest.case_count, "coverage_area_km2": round(closest.coverage_area_km2, 2),
                    "note": "最近历史运行中的同病种最近聚类，仅供趋势线索，不证明为同一传播链"}
    return None


def build_strategy_context(session: Session, run_id: int, cluster_id: int) -> StrategyContext:
    run, reports, _ = build_cluster_reports(session, run_id)
    report = next((item for item in reports if item.cluster_id == cluster_id), None)
    if report is None:
        raise StrategyError(f"运行 {run_id} 中不存在聚类 {cluster_id}")
    sources = [document_json(item) for item in search_knowledge(session, disease=report.illness)]
    if not sources:
        raise StrategyError(f"知识库中没有疾病“{report.illness}”的现行资料")
    end = run.window_end.date()
    return StrategyContext(
        report=report,
        run={"run_id": run.run_id, "window_start": run.window_start.date().isoformat(), "window_end_exclusive": end.isoformat(),
             "window_days": run.window_days, "eps_meters": run.eps_meters, "min_points": run.min_points,
             "include_proxy_locations": run.include_proxy_locations, "case_count": run.case_count,
             "cluster_count": run.cluster_count},
        calendar={"analysis_date": end.isoformat(), "year": end.year, "month": end.month, "season": _season(end.month),
                  "seasonality_warning": "季节信息是背景变量；不得在缺少病种和属地证据时宣称季节性因果关系"},
        location=_nearest_city(report), previous_comparison=_previous_cluster(session, run, report), sources=sources,
    )


STRATEGY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "title": {"type": "string"}, "assessment": {"type": "string"},
        "situation_level": {"type": "string", "enum": ["常规关注", "加强监测", "优先响应", "信息不足"]},
        "reasoning_summary": {"type": "array", "items": {"type": "string"}},
        "priorities": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"priority": {"type": "string"}, "action": {"type": "string"}, "rationale": {"type": "string"},
                           "citations": {"type": "array", "items": {"type": "string"}}},
            "required": ["priority", "action", "rationale", "citations"]}},
        "additional_information_needed": {"type": "array", "items": {"type": "string"}},
        "data_quality": {"type": "array", "items": {"type": "string"}},
        "escalation": {"type": "string"}, "disclaimer": {"type": "string"},
    },
    "required": ["title", "assessment", "situation_level", "reasoning_summary", "priorities", "additional_information_needed", "data_quality", "escalation", "disclaimer"],
}


def _agent_input(context: StrategyContext) -> tuple[str, list[dict]]:
    evidence = []
    for document in context.sources:
        for index, section in enumerate(document["sections"], 1):
            evidence.append({"evidence_id": f"文献{document['document_id']}-{index}", "disease": document["disease"],
                             "title": document["title"], "version": document["version"], "section": section["title"],
                             "content": section["content"], "source_url": document["source_url"]})
    report = context.report.to_dict()
    report.pop("centroid_latitude", None)
    report.pop("centroid_longitude", None)
    aggregate = {"cluster_report": report, "run": context.run, "calendar": context.calendar,
                 "location": context.location, "previous_comparison": context.previous_comparison, "evidence": evidence}
    return json.dumps(aggregate, ensure_ascii=False), evidence


def generate_strategy_brief(context: StrategyContext, *, opener=None) -> dict:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise StrategyError("尚未配置 OPENROUTER_API_KEY；请先创建免费的 OpenRouter API Key")
    model = os.environ.get("OPENROUTER_MODEL", "openrouter/free").strip()
    agent_input, evidence = _agent_input(context)
    instructions = """你是中国公共卫生聚集事件研判智能体。只根据提供的聚类汇总和知识证据形成中文策略，不得虚构事实、法规或引用。区分观察事实、推断和建议；考虑时间窗口、季节背景、城市级位置、人口学、空间范围、代理位置、数据缺失和历史比较。每项行动的 citations 只能使用输入中的 evidence_id；如果证据不足就写入 additional_information_needed。不要输出患者级信息，不作临床诊断，不宣称空间覆盖范围等于传播边界。"""
    payload = {"model": model, "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": agent_input}],
               "response_format": {"type": "json_schema", "json_schema": {"name": "public_health_strategy", "strict": True, "schema": STRATEGY_SCHEMA}},
               "provider": {"require_parameters": True, "data_collection": "deny"}, "temperature": 0.2}
    request = Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(payload).encode(),
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                               "HTTP-Referer": "http://127.0.0.1:8000", "X-Title": "Public Health Clustering Explorer"}, method="POST")
    opener = opener or urlopen
    try:
        with opener(request, timeout=90) as response:
            result = json.loads(response.read().decode())
        brief = json.loads(result["choices"][0]["message"]["content"])
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise StrategyError(f"智能体服务请求失败（HTTP {exc.code}）：{detail}") from exc
    except (URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise StrategyError(f"智能体服务返回无效结果：{exc}") from exc
    allowed = {item["evidence_id"] for item in evidence}
    if any(not set(item["citations"]).issubset(allowed) for item in brief["priorities"]):
        raise StrategyError("智能体返回了知识库中不存在的引用，结果已拦截")
    brief.update({"mode": f"模型智能体 · {result.get('model', model)}", "evidence": evidence})
    return brief
