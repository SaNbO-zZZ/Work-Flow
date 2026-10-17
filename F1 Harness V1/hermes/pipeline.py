"""每期视频的状态流转（B2 定稿）：四道关卡、各工作阶段的步骤批次、打回去向。"""

WORK_STAGES = ["选题与资料", "写逐字稿", "配音", "配素材与粗剪", "合成与QA", "经验总结", "上传Drive"]

# 主线顺序；关卡状态以"待审"结尾
FLOW = [
    "排队",
    "选题与资料", "G0待审",
    "写逐字稿", "G1待审",
    "配音", "配素材与粗剪", "G2待审",
    "合成与QA", "G3待审",
    "经验总结", "待发布",   # 待发布：展示成果，等用户取文件、手动点“上传 Drive”（用户 2026-10-13 定）
    "上传Drive", "归档倒计时", "本地已清理",
]
GATE_OF_STATE = {"G0待审": "G0", "G1待审": "G1", "G2待审": "G2", "G3待审": "G3"}
STATE_OF_GATE = {v: k for k, v in GATE_OF_STATE.items()}

# 打回时允许退回的阶段，第一个是默认
REJECT_TARGETS = {
    "G0": ["选题与资料"],
    "G1": ["写逐字稿"],
    "G2": ["配素材与粗剪", "配音"],
    "G3": ["配素材与粗剪", "配音", "写逐字稿"],
}

OTHER_STATES = {"闲聊", "暂停", "已取消", "需人工"}
TERMINAL_STATES = {"已取消", "本地已清理"}

# 每个阶段：按顺序执行的批次；批次内并行。(步骤, 角色, 是否占用剪映)
# 角色"程序"表示不调用模型的本地处理
STAGE_PHASES: dict[str, list[list[tuple[str, str, bool]]]] = {
    "选题与资料": [
        [("D1资料与事实", "资料", False), ("D5梗与出处", "梗核实", False)],
        [("总控汇总选题", "总控", False)],
    ],
    "写逐字稿": [[("写逐字稿", "文案", False)]],
    "配音": [
        [("剪映生成配音", "配音操作", True)],
        [("配音后处理", "程序", False)],
    ],
    "配素材与粗剪": [
        [("D2找素材", "找素材", False), ("D4图形与叠层", "图形", False)],
        [("D3素材打标", "素材打标", False)],
        [("D6剪辑决策", "剪辑决策", False)],
        [("粗剪渲染", "程序", False)],
    ],
    "合成与QA": [
        [("合成", "程序", False)],
        [("硬检查", "程序", False)],
        [("QA评审", "QA评审", False)],
    ],
    "经验总结": [[("经验总结", "总控", False)]],
    "上传Drive": [[("上传与核对", "程序", False)]],
}


def next_state(state: str) -> str:
    i = FLOW.index(state)
    if i + 1 >= len(FLOW):
        raise ValueError(f"{state} 之后没有下一个状态")
    return FLOW[i + 1]


def reject_target(gate: str, back_to: str | None) -> str:
    targets = REJECT_TARGETS[gate]
    if back_to is None:
        return targets[0]
    if back_to not in targets:
        raise ValueError(f"{gate} 只能打回到 {targets}")
    return back_to
