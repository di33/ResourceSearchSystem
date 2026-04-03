from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class AcceptanceCategory(str, Enum):
    SECURITY = "security"
    FAULT_TOLERANCE = "fault_tolerance"
    PERFORMANCE = "performance"
    QUALITY = "quality"
    INTEGRATION = "integration"


@dataclass
class AcceptanceItem:
    """单个验收项。"""
    id: str
    category: AcceptanceCategory
    title: str
    description: str
    target: str
    verification_method: str
    passed: Optional[bool] = None
    notes: str = ""


@dataclass
class AcceptanceChecklist:
    """验收清单。"""
    items: List[AcceptanceItem] = field(default_factory=list)

    def add(self, item: AcceptanceItem):
        self.items.append(item)

    def pass_rate(self) -> float:
        evaluated = [i for i in self.items if i.passed is not None]
        if not evaluated:
            return 0.0
        return sum(1 for i in evaluated if i.passed) / len(evaluated)

    def by_category(self, category: AcceptanceCategory) -> list[AcceptanceItem]:
        return [i for i in self.items if i.category == category]

    def pending_items(self) -> list[AcceptanceItem]:
        return [i for i in self.items if i.passed is None]

    def failed_items(self) -> list[AcceptanceItem]:
        return [i for i in self.items if i.passed is False]

    def summary(self) -> dict:
        total = len(self.items)
        passed = sum(1 for i in self.items if i.passed is True)
        failed = sum(1 for i in self.items if i.passed is False)
        pending = sum(1 for i in self.items if i.passed is None)
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pending": pending,
            "pass_rate": self.pass_rate(),
        }


def build_default_checklist() -> AcceptanceChecklist:
    """构建默认验收清单，对齐设计文档第7-10章。"""
    cl = AcceptanceChecklist()

    # Security
    cl.add(AcceptanceItem(
        id="SEC-01", category=AcceptanceCategory.SECURITY,
        title="密钥本地存储",
        description="本地密钥不上传云端，优先使用平台原生密钥管理",
        target="密钥仅存储在本地安全存储中",
        verification_method="审查密钥存储代码和配置",
    ))
    cl.add(AcceptanceItem(
        id="SEC-02", category=AcceptanceCategory.SECURITY,
        title="HTTPS + JWT 访问控制",
        description="业务接口统一使用 HTTPS + JWT 认证",
        target="所有 API 调用使用 HTTPS",
        verification_method="审查 API 客户端配置",
    ))
    cl.add(AcceptanceItem(
        id="SEC-03", category=AcceptanceCategory.SECURITY,
        title="Agent 权限最小化",
        description="Agent 服务账号仅开放检索与下载权限",
        target="Agent 无法调用注册、提交、删除接口",
        verification_method="权限矩阵测试",
    ))
    cl.add(AcceptanceItem(
        id="SEC-04", category=AcceptanceCategory.SECURITY,
        title="审计日志",
        description="注册、提交、删除等操作记录审计日志",
        target="关键操作 100% 有审计记录",
        verification_method="审查 process_log 表和日志输出",
    ))

    # Fault tolerance
    cl.add(AcceptanceItem(
        id="FT-01", category=AcceptanceCategory.FAULT_TOLERANCE,
        title="单资源失败不阻断批量",
        description="单个资源处理失败不影响其他资源继续处理",
        target="批量中失败资源隔离",
        verification_method="模拟单个资源失败，验证其他资源正常完成",
    ))
    cl.add(AcceptanceItem(
        id="FT-02", category=AcceptanceCategory.FAULT_TOLERANCE,
        title="错误码与错误信息记录",
        description="每个阶段失败记录错误码和错误信息",
        target="失败任务都有 error_code 和 error_message",
        verification_method="检查 local_cache 中失败任务的字段",
    ))
    cl.add(AcceptanceItem(
        id="FT-03", category=AcceptanceCategory.FAULT_TOLERANCE,
        title="失败资源重新入队",
        description="失败资源可通过 get_retry_candidates 获取并重新处理",
        target="retry_count < max_retries 的失败资源可重试",
        verification_method="调用 get_retry_candidates 并验证结果",
    ))
    cl.add(AcceptanceItem(
        id="FT-04", category=AcceptanceCategory.FAULT_TOLERANCE,
        title="断点续传/恢复",
        description="中断后可从断点继续处理",
        target="get_resumable_tasks 返回可恢复任务",
        verification_method="中断模拟后检查恢复列表",
    ))
    cl.add(AcceptanceItem(
        id="FT-05", category=AcceptanceCategory.FAULT_TOLERANCE,
        title="幂等注册与提交",
        description="重复注册和提交不产生副作用",
        target="相同 idempotency_key 返回一致结果",
        verification_method="重复调用验证结果一致",
    ))

    # Performance
    cl.add(AcceptanceItem(
        id="PERF-01", category=AcceptanceCategory.PERFORMANCE,
        title="单资源处理耗时",
        description="描述 + 向量生成 P95 ≤ 4 秒",
        target="P95 ≤ 4s",
        verification_method="批量测试统计 P95",
    ))
    cl.add(AcceptanceItem(
        id="PERF-02", category=AcceptanceCategory.PERFORMANCE,
        title="检索响应时间",
        description="十万级资源检索 P95 ≤ 2 秒",
        target="P95 ≤ 2s",
        verification_method="压测评估",
    ))

    # Quality
    cl.add(AcceptanceItem(
        id="QUAL-01", category=AcceptanceCategory.QUALITY,
        title="描述格式合格率",
        description="两段式描述格式合格率 ≥ 99%",
        target="≥ 99%",
        verification_method="批量样本校验",
    ))
    cl.add(AcceptanceItem(
        id="QUAL-02", category=AcceptanceCategory.QUALITY,
        title="预览质量校验",
        description="预览载体通过质量校验（非全黑/全白/尺寸/体积）",
        target="validate_preview 通过率",
        verification_method="批量预览生成后校验",
    ))
    cl.add(AcceptanceItem(
        id="QUAL-03", category=AcceptanceCategory.QUALITY,
        title="向量维度一致性",
        description="所有生成的向量维度与模型配置一致",
        target="100% 维度一致",
        verification_method="validate_embedding 全量校验",
    ))

    # Integration
    cl.add(AcceptanceItem(
        id="INT-01", category=AcceptanceCategory.INTEGRATION,
        title="本地状态机完整闭环",
        description="discovered → preview_ready → description_ready → embedding_ready → package_ready → registered → uploaded → committed → synced",
        target="状态机全路径可达",
        verification_method="单资源端到端测试",
    ))
    cl.add(AcceptanceItem(
        id="INT-02", category=AcceptanceCategory.INTEGRATION,
        title="Agent 检索预览联调",
        description="Agent 可通过 search_digital_resource_preview 获取搜索结果和预览",
        target="Agent 工具返回正确结构",
        verification_method="Mock 联调测试",
    ))
    cl.add(AcceptanceItem(
        id="INT-03", category=AcceptanceCategory.INTEGRATION,
        title="Agent 下载联调",
        description="Agent 可通过 get_digital_resource_download_link 获取下载链接",
        target="Agent 工具返回有效下载链接",
        verification_method="Mock 联调测试",
    ))

    return cl


@dataclass
class DeliveryStage:
    """阶段交付物。"""
    stage: int
    name: str
    objective: str
    exit_criteria: str
    specs: List[str]


def build_delivery_plan() -> List[DeliveryStage]:
    """根据设计文档第10章构建阶段交付计划。"""
    return [
        DeliveryStage(
            stage=1, name="描述生成模块与样本验证",
            objective="完成 LLM 描述生成与校验",
            exit_criteria="两段式输出稳定，格式合格率达标",
            specs=["Spec 1", "Spec 2", "Spec 3", "Spec 4"],
        ),
        DeliveryStage(
            stage=2, name="Embedding 与本地缓存闭环",
            objective="完成向量生成和本地持久化",
            exit_criteria="本地状态机跑通，可断点恢复",
            specs=["Spec 5", "Spec 6", "Spec 7"],
        ),
        DeliveryStage(
            stage=3, name="注册、上传、提交协议联调",
            objective="打通云端入库全流程",
            exit_criteria="单资源与批量入库均可成功",
            specs=["Spec 8", "Spec 9"],
        ),
        DeliveryStage(
            stage=4, name="检索与下载接口",
            objective="消费侧检索预览与下载",
            exit_criteria="Agent 可完成检索预览与下载",
            specs=["Spec 10", "Spec 11"],
        ),
        DeliveryStage(
            stage=5, name="评估与压测",
            objective="检索质量和性能验证",
            exit_criteria="检索质量、性能、容错验证通过",
            specs=["Spec 12"],
        ),
        DeliveryStage(
            stage=6, name="生产化监控与审计",
            objective="告警、日志、巡检上线",
            exit_criteria="告警、日志、巡检任务生效",
            specs=["Spec 12"],
        ),
    ]
