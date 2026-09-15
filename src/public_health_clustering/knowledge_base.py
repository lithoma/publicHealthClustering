"""Curated, versioned Chinese disease knowledge for local decision support."""

from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .models import KnowledgeDocument, KnowledgeDocumentStatus, KnowledgeSection


SEED_DOCUMENTS = (
    {
        "disease": "百日咳", "title_zh": "百日咳防控方案（2024年版）",
        "issuing_body": "国家疾控局、国家卫生健康委", "publication_date": date(2024, 5, 24),
        "version_label": "2024年版",
        "source_url": "https://www.ndcpa.gov.cn/jbkzzx/c100014/common/content/content_1794296609501982720.html",
        "sections": (
            ("监测与报告", "落实早发现、早报告、早隔离、早治疗；结合病例报告、医疗机构和重点人群监测研判疫情。", "监测 病例报告 四早"),
            ("聚集性疫情处置", "发现聚集性疫情后开展流行病学调查、密切接触者管理、重点机构防控和必要的实验室检测。", "聚集 疫情 调查 密切接触者"),
            ("重点人群", "关注婴幼儿、孕产妇及托幼机构、学校等重点场所；按国家免疫程序推进预防接种。", "婴幼儿 孕产妇 托幼 学校 接种"),
        ),
    },
    {
        "disease": "麻疹", "title_zh": "麻疹诊疗方案（2024年版）",
        "issuing_body": "国家卫生健康委、国家中医药局", "publication_date": date(2024, 7, 29),
        "version_label": "2024年版",
        "source_url": "https://www.gov.cn/zhengce/zhengceku/202407/P020240729756402437679.pdf",
        "sections": (
            ("流行病学要点", "麻疹是急性呼吸道传染病，人是麻疹病毒的唯一宿主；分析时应关注发热出疹病例的时间、地点与接触史。", "呼吸道 发热 出疹 接触史"),
            ("病例识别", "常见表现包括发热、流涕、咳嗽、流泪、畏光和斑丘疹；疑似病例应由专业机构按现行规范诊断和报告。", "发热 咳嗽 斑丘疹 疑似病例"),
            ("防控提示", "聚集事件研判应结合免疫史、接触关系和实验室结果；本系统的空间聚类不能替代病例诊断或传播链判定。", "免疫史 实验室 聚集 传播链"),
        ),
    },
    {
        "disease": "流感", "title_zh": "流行性感冒诊疗方案（2025年版）",
        "issuing_body": "国家卫生健康委、国家中医药局", "publication_date": date(2025, 1, 20),
        "version_label": "2025年版",
        "source_url": "https://www.nhc.gov.cn/ylyjs/zcwj/202501/f8fcecca59a048bebc4a71847ce57594/files/1741764832851_94226.pdf",
        "sections": (
            ("流行病学要点", "甲型和乙型流感病毒可呈季节性流行；患者和隐性感染者是主要传染源，应结合流感季和接触史研判。", "季节性 传染源 接触史"),
            ("重点人群", "关注儿童、老年人、孕妇、慢性基础疾病患者以及养老、托幼、学校等重点场所人群。", "儿童 老年人 孕妇 慢性病 重点场所"),
            ("处置提示", "病例聚集时加强症状监测、通风与个人防护，及时开展病原学检查；重症风险和临床处置应由医疗专业人员判断。", "聚集 通风 防护 病原学 重症"),
        ),
    },
)


def seed_chinese_knowledge_base(session: Session) -> int:
    """Idempotently add the initial official-source Chinese knowledge set."""
    created = 0
    for item in SEED_DOCUMENTS:
        existing = session.scalar(select(KnowledgeDocument).where(
            KnowledgeDocument.source_url == item["source_url"],
            KnowledgeDocument.version_label == item["version_label"],
        ))
        if existing:
            continue
        document = KnowledgeDocument(**{key: value for key, value in item.items() if key != "sections"})
        session.add(document)
        session.flush()
        for order, (title, content, keywords) in enumerate(item["sections"]):
            session.add(KnowledgeSection(document_id=document.document_id,
                                         section_title_zh=title, content_zh=content,
                                         keywords_zh=keywords, sort_order=order))
        created += 1
    return created


def search_knowledge(session: Session, *, query: str | None = None,
                     disease: str | None = None, include_inactive: bool = False) -> list[KnowledgeDocument]:
    statement = select(KnowledgeDocument).options(selectinload(KnowledgeDocument.sections))
    if not include_inactive:
        statement = statement.where(KnowledgeDocument.status == KnowledgeDocumentStatus.CURRENT)
    if disease:
        statement = statement.where(KnowledgeDocument.disease == disease)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        statement = statement.join(KnowledgeDocument.sections, isouter=True).where(or_(
            KnowledgeDocument.disease.ilike(pattern), KnowledgeDocument.title_zh.ilike(pattern),
            KnowledgeDocument.issuing_body.ilike(pattern), KnowledgeSection.section_title_zh.ilike(pattern),
            KnowledgeSection.content_zh.ilike(pattern), KnowledgeSection.keywords_zh.ilike(pattern),
        )).distinct()
    return list(session.scalars(statement.order_by(KnowledgeDocument.disease, KnowledgeDocument.publication_date.desc())).unique())


def document_json(document: KnowledgeDocument) -> dict:
    return {
        "document_id": document.document_id, "disease": document.disease,
        "title": document.title_zh, "issuing_body": document.issuing_body,
        "jurisdiction": document.jurisdiction, "publication_date": document.publication_date.isoformat(),
        "version": document.version_label, "source_url": document.source_url,
        "status": document.status.value,
        "sections": [{"title": section.section_title_zh, "content": section.content_zh,
                      "keywords": section.keywords_zh} for section in document.sections],
    }
