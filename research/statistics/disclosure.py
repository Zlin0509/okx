"""Generate the two P2 errata documents from the machine result, never hand-copy p-values."""

from pathlib import Path


def rw_disclosure(result):
    batch = result["batch"]
    raw = batch["analyses"]["rw_residual"]["tests"]["rw_residual:eg"]["pvalue"]
    holm = batch["adjustments"]["EG_Holm"]["rw_residual:eg"]["adjusted_p"]
    by = batch["adjustments"]["EG_BY_diagnostic_not_license"]["rw_residual:eg"]["adjusted_p"]
    status = batch["permits"]["rw_residual"]["status"]
    truth = result["generator_truth_price_relation"]["rw_residual"]
    return (
        "<!-- generated: research.statistics.disclosure; input: statistics JSON -->\n"
        "| rw_residual 指标 | 机器结果 |\n|---|---|\n"
        f"| EG 原始 p | `{raw!r}` |\n"
        f"| EG Holm 调整 p（主筛查） | `{holm!r}` |\n"
        f"| EG BY 调整 p（仅对照） | `{by!r}` |\n"
        f"| 统计筛查 | `{status}` |\n"
        f"| 生成器关系真值（仅评估注释） | `{str(truth).lower()}` |\n\n"
        "该固定合成负例保留为假阳性；真值只在检验和模型许可之后用于评估，"
        "不参与筛查、阈值或参数选择。没有更换种子、阈值或 qualified 结果。\n"
    )


def documents(result):
    disclosure = rw_disclosure(result)
    common = (
        "\n旧 M3B_REPORT.md、M3B_INTEGRATION.md 及旧评论的 Holm≈0.038652 为抄录错误；"
        "旧文件及冻结保留，本版本勘误。旧机器统计值和筛查判定无需更改。\n\n"
        "价格和相对收益模型各自携带不可变 provenance：origin=synthetic_test、"
        "source=synthetic_statistical_screen_only、screen_only=true、allowed_use=synthetic_calculation，"
        "并绑定候选、决策时间及计划／完整族／训练摘要。单独 dataclasses.asdict／JSON／pickle "
        "仍保留标签；显式请求真实 origin 或 trading／profit／deployment 用途被拒绝。"
        "生成器真值和 false_positive 不进入模型或许可逻辑。标签和接口拒绝不构成 Python 进程安全沙箱，"
        "也不能辨认调用者谎报的来源或标签被外部主动剥除的副本。\n\n"
        "统计有限样本会出错，screen-only 模型不是真实可交易资格、收益／容量或上线证据。"
        "快照／完整族绑定检查及原统计规格不变；本版不运行真实训练、选参或样本外。"
        "应用并发 P1、数据、发布／共享及 PR／CI 阻断继续保留。\n"
    )
    return {
        "M3B_REPORT_R2.md": "# M3B P2 披露及模型追溯勘误报告\n\n" + disclosure + common,
        "M3B_INTEGRATION_R2.md": (
            "# M3B P2 整合索引\n\n"
            "本文件补充旧整合文档，统一源冻结为 M3B_SOURCE_FREEZE_V3.json。"
            "M3 合成黄金和旧统计参考保留；新模型参数／标签另存 M3B_EXPECTED_R2.json。"
            "CLI 在核对新参考前，删除新增模型参数字段后与旧参考逐值比对，完整 batch 和旧标量不变。"
            "两类模型始终携带同一许可的追溯字段。\n\n"
            + disclosure
            + common
            + "\n复现：按原独立哈希锁单安装依赖后运行 `python3 -m research.statistics verify` "
            "及 `python3 -m research.m3 verify`。统计 CLI 同时检查两份生成文档与机器结果一致；"
            "使用新目录 research/results/m3b_r2，拒绝覆盖。"
            "Guard DNS／connect／snapshot 计数、当前进程及线程范围与旧整合版本相同，"
            "不覆盖 subprocess、native C、直接系统调用或 OS 隔离。\n"
        ),
    }


def check_documents(root, result):
    for name, expected in documents(result).items():
        actual = (Path(root) / "research" / name).read_text(encoding="utf-8")
        if actual != expected:
            from ..engine import Rejected

            raise Rejected("disclosure_mismatch", name)
