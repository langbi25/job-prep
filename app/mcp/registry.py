"""工具注册表：全系统唯一的工具元数据来源。

为什么要有这一层：此前同一批能力有三份手写清单——`harness.TOOLS`（6 个读）、
`agent_tools.SCHEMAS`（11 个动作）、`gateway.SPECS`（对外 9 个，摘要另写一遍）。
三份各自维护描述，改一个工具要同步三处，漏一处就出现「模型看到的说明与实际行为不符」。
这个项目历史上最痛的几次事故都是口径分裂，工具清单是最后一块没统一的地方。

现在 `REGISTRY` 是唯一事实源，两个出口各按 `surfaces` 过滤：

- `surface="agent"` → 内置 harness 的 17 个（细粒度，含采集确认、记忆读写、结果翻页）；
- `surface="mcp"`   → MCP 网关对外的 9 个（读 6 写 3，粒度按「一个对话回合能完成的任务」切）。

两条约定：

1. **只存元数据，不存 handler**。实现仍在 `harness._IMPL` / `agent_tools.IMPL`，由调用方
   按 name 绑定并用 `missing_handlers` 校验。这样避免 registry → harness → registry 的循环导入。
2. **`parameters` 是从旧清单机械导出的一次性快照**，不是手抄。新增或修改工具参数改这里；
   `harness.TOOLS` 与 `agent_tools.SCHEMAS` 已改为从本模块派生，不要再往里写字面量。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDef:
    """一个工具的元数据。handler 不在这里——见模块 docstring 第 1 条。"""

    name: str
    description: str          # 给模型看：什么时候用、不要用它做什么（这段本身就是 prompt）
    summary: str              # 给人和面板看：一句话，MCP catalog 与工具清单都用它
    read_only: bool
    surfaces: tuple[str, ...]  # "agent" = 内置循环可见；"mcp" = 对外网关暴露
    parameters: dict

    def schema(self) -> dict:
        """OpenAI function-calling 形状。MCP 的 inputSchema 与它是同一套 JSON Schema，
        所以内置循环和 MCP 客户端可以共用同一份定义，不需要翻译层。"""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


REGISTRY: dict[str, ToolDef] = {
    'get_daily_brief': ToolDef(
        name='get_daily_brief',
        description='获取候选人当前学习概况：到期卡片数、未解决错题数、近期模拟面试均分、薄弱卡片',
        summary='今天的学习概况：到期卡、未解决错题、近期面试均分、薄弱卡',
        read_only=True,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object', 'properties': {}},
    ),
    'list_due_cards': ToolDef(
        name='list_due_cards',
        description='列出今日到期的复习卡片（含主题和掌握度）',
        summary='列出今日到期的复习卡片（含主题与掌握度）',
        read_only=True,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object', 'properties': {'limit': {'type': 'integer', 'description': '最多返回条数'}}},
    ),
    'get_weak_topics': ToolDef(
        name='get_weak_topics',
        description='获取薄弱主题（低掌握度卡片 + 近 14 天错题主题），可按方向过滤',
        summary='薄弱主题：低掌握度卡片 + 近 14 天错题主题，可按方向过滤',
        read_only=True,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object',
         'properties': {'direction': {'type': 'string', 'enum': ['backend', 'bigdata', 'agent']}}},
    ),
    'get_top_questions': ToolDef(
        name='get_top_questions',
        description='获取面经题池中的高频题目（按出现次数排序）',
        summary='题目池高频题（按面经出现次数排序），代表真实考点分布',
        read_only=True,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object', 'properties': {'limit': {'type': 'integer'}}},
    ),
    'search_kb': ToolDef(
        name='search_kb',
        description="在知识库里做混合检索，返回相关卡片（标题/主题/掌握度/要点原文）。需要回忆'我到底记过什么'时用它，不要凭记忆编内容。没命中说明是知识缺口。",
        summary='在个人知识库里做混合检索（关键词 + 向量），返回相关卡片要点',
        read_only=True,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object',
         'properties': {'query': {'type': 'string', 'description': '检索语句，可用题目原文或关键词'},
                        'topic': {'type': 'string', 'description': '限定主题（可选），如 Flink / Agent 记忆'},
                        'top_k': {'type': 'integer', 'description': '返回卡片数，默认 3，最多 5'}},
         'required': ['query']},
    ),
    'get_card': ToolDef(
        name='get_card',
        description='按 card_id 取一张知识卡的完整内容（要点/常见坑/覆盖的题）。search_kb 只回摘要，要读全文时调这个。',
        summary='按 card_id 取一张知识卡的完整内容（要点/常见坑/覆盖的题）',
        read_only=True,
        surfaces=('mcp',),
        parameters={'type': 'object',
         'properties': {'card_id': {'type': 'string',
                                    'description': '知识卡 id，形如 kb-backend-gen-001；一般来自 search_kb '
                                                   '结果'}},
         'required': ['card_id']},
    ),
    'list_open_mistakes': ToolDef(
        name='list_open_mistakes',
        description='列出未解决的错题（含错因和主题）',
        summary='未解决的错题（含错因与主题），复盘时取它',
        read_only=True,
        surfaces=('agent',),
        parameters={'type': 'object', 'properties': {'limit': {'type': 'integer'}}},
    ),
    'list_recent_ingests': ToolDef(
        name='list_recent_ingests',
        description="最近几次采集的状态，用于回答'刚才那份处理到哪了'。",
        summary='最近几次采集的处理状态',
        read_only=True,
        surfaces=('agent',),
        parameters={'type': 'object', 'properties': {'limit': {'type': 'integer', 'description': '默认 5'}}},
    ),
    'list_pending_actions': ToolDef(
        name='list_pending_actions',
        description='待审批的 Agent 动作（建卡/合并/归档/提前复习）。用于提醒用户去审批中心。',
        summary='待审批的 Agent 动作（建卡/合并/归档/提前复习）',
        read_only=True,
        surfaces=('agent',),
        parameters={'type': 'object', 'properties': {'limit': {'type': 'integer', 'description': '默认 10'}}},
    ),
    'recall_memory': ToolDef(
        name='recall_memory',
        description="按主题检索关于用户的既有记忆。需要确认'他之前说过什么'时用；不要凭印象假设用户的偏好或目标。",
        summary='按主题检索关于用户的既有记忆',
        read_only=True,
        surfaces=('agent',),
        parameters={'type': 'object',
         'properties': {'query': {'type': 'string', 'description': '检索语句，如 目标岗位 / 讲题偏好 / 面试安排'},
                        'top_k': {'type': 'integer', 'description': '返回条数，默认 5'}},
         'required': ['query']},
    ),
    'fetch_tool_result': ToolDef(
        name='fetch_tool_result',
        description='取回之前某个工具被截断的结果片段。工具返回里出现 _ref 字段时说明结果太长已分片，用这个工具翻页，不要重新调用原工具（会浪费一轮且可能拿到同样被截断的结果）。',
        summary='取回之前被截断的工具结果分片（按 _ref 翻页，别重调原工具）',
        read_only=True,
        surfaces=('agent',),
        parameters={'type': 'object',
         'properties': {'ref': {'type': 'string', 'description': '工具返回里的 _ref，形如 tr-xxxxxxxx'},
                        'offset': {'type': 'integer', 'description': '从第几个字符开始，默认 0'},
                        'limit': {'type': 'integer', 'description': '取多少字符，默认 1500，最多 4000'}},
         'required': ['ref']},
    ),
    'ingest_text': ToolDef(
        name='ingest_text',
        description="投喂一份面经原文：存档→抽取题目→过质量闸门→自动入池→起草待审卡片。用户粘贴面经、说'把这段收进去'时调用。返回 ingest_id / 题目数 / 是否自动入池 / 待审卡数。",
        summary='投喂面经原文：存档→抽题→过质量闸门→入池→起草待审卡（写操作）',
        read_only=False,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object',
         'properties': {'text': {'type': 'string', 'description': '面经原文全文，不要截断、不要总结'},
                        'source_type': {'type': 'string',
                                        'enum': ['paste', 'url', 'manual'],
                                        'description': '来源类型，粘贴用 paste'},
                        'max_cards': {'type': 'integer', 'description': '自动起草几张卡，默认 2，最多 3'},
                        'draft_cards': {'type': 'boolean', 'description': '入池后是否顺手起草卡，默认 true'}},
         'required': ['text']},
    ),
    'confirm_ingest': ToolDef(
        name='confirm_ingest',
        description="对已被质量闸门拦下、停在 extracted 状态的材料执行入池。只有用户明确说'收/确认入池'时才调用。",
        summary='把被质量闸门拦下、停在 extracted 的材料确认入池（写）',
        read_only=False,
        surfaces=('agent',),
        parameters={'type': 'object',
         'properties': {'ingest_id': {'type': 'string',
                                      'description': '采集 id，形如 ing-20261104-231500-1234'}},
         'required': ['ingest_id']},
    ),
    'draft_cards': ToolDef(
        name='draft_cards',
        description='为题目池里的题起草知识卡，产出待审批动作（不会直接成为卡片）。按主题补卡传 topic，按具体题补卡传 question_ids，二者至少给一个。',
        summary='为题目起草知识卡（进审批队列）：按主题传 topic，按题传 question_ids',
        read_only=False,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object',
         'properties': {'topic': {'type': 'string',
                                  'description': '主题，如「Flink 数据倾斜」。用户说「给XX补卡」时必须传这个，否则会从全池抓题、补错主题'},
                        'question_ids': {'type': 'array',
                                         'items': {'type': 'string'},
                                         'description': '题目 id 列表，最多 3 个'},
                        'limit': {'type': 'integer', 'description': '最多起草几张，默认 2'},
                        'only_uncarded': {'type': 'boolean', 'description': '只挑还没有卡的题，默认 true'}}},
    ),
    'ingest_retrospective': ToolDef(
        name='ingest_retrospective',
        description="复盘文档一条龙：提炼面试考点→生成题目入池→起草知识卡（卡只进审批队列，不直接落库）。用户说'把这份复盘变成题目和卡片'时调用。",
        summary='复盘文档一条龙：提炼面试考点→题目入池→起草待审卡（写操作，产物走审批）',
        read_only=False,
        surfaces=('agent', 'mcp'),
        parameters={'type': 'object',
         'properties': {'content': {'type': 'string', 'description': '复盘文档全文（与 source_path 二选一）'},
                        'source_path': {'type': 'string',
                                        'description': '文档相对项目根的路径，如 docs/复盘-自进化闭环.md'},
                        'title': {'type': 'string', 'description': '文档标题，缺省取首行'},
                        'build_cards': {'type': 'boolean', 'description': '是否顺手起草卡（进审批），默认 true'},
                        'card_limit': {'type': 'integer', 'description': '最多起草几张，默认 2'}},
         'required': []},
    ),
    'analyze_coverage': ToolDef(
        name='analyze_coverage',
        description='生成/刷新一份面经的作战地图：逐题覆盖、预测档位、缺口清单。不给 id 就用最近一场。',
        summary='生成/刷新一份面经的作战地图：逐题覆盖、预测档位、缺口（写）',
        read_only=False,
        surfaces=('agent',),
        parameters={'type': 'object',
         'properties': {'interview_id': {'type': 'string', 'description': '面经 id，形如 2026-11-0002'}}},
    ),
    'make_study_plan': ToolDef(
        name='make_study_plan',
        description='跑一轮学习教练 Agent，产出今天的学习计划（它会自己查到期卡/错题/薄弱主题）。',
        summary='跑一轮学习教练：查到期卡/错题/薄弱项，产出今日计划（写）',
        read_only=False,
        surfaces=('agent',),
        parameters={'type': 'object',
         'properties': {'direction': {'type': 'string', 'enum': ['backend', 'bigdata', 'agent']}}},
    ),
    'remember': ToolDef(
        name='remember',
        description="记住关于用户的一条长期信息（事实/偏好/任务状态）。用户说'以后都''记住''我的目标是'或明确纠正你之前理解时调用。同一主题用同一个 key，会覆盖旧值而不是堆两条。不要记一次性的查询结果或数字统计——那些现查更准。",
        summary='记住一条关于用户的长期信息：事实/偏好/任务状态（写）',
        read_only=False,
        surfaces=('agent',),
        parameters={'type': 'object',
         'properties': {'kind': {'type': 'string',
                                 'enum': ['fact', 'preference', 'task_state', 'episode'],
                                 'description': 'fact 事实 / preference 偏好 / task_state 进行中任务 / '
                                                'episode 经过结论'},
                        'key': {'type': 'string', 'description': '2~10 字主题词，如 目标岗位、面试时间、偏好-讲解粒度'},
                        'value': {'type': 'string', 'description': '一句话事实，不超过 60 字'},
                        'scope': {'type': 'string',
                                  'enum': ['session', 'global'],
                                  'description': 'global=以后每次对话都生效；session=只对本次对话有效（默认）'},
                        'ttl_days': {'type': 'integer', 'description': '多少天后自动过期，不填按类型默认'}},
         'required': ['kind', 'key', 'value']},
    ),
}


def get(name: str) -> ToolDef | None:
    return REGISTRY.get(name)


def names(surface: str | None = None) -> list[str]:
    """某暴露面的工具名，顺序即注册顺序（也是 tools/list 的返回顺序）。"""
    return [n for n, d in REGISTRY.items() if surface is None or surface in d.surfaces]


def schemas(*, surface: str | None = None, only=None) -> list[dict]:
    """OpenAI 形状的工具清单。

    `only` 传一个可迭代的工具名（通常是某个模块的 impl 字典的 key）：只保留**同时**属于
    该实现集合与指定暴露面的工具。这样"某模块有哪些工具"由它的实现决定，不必再手写第二份名单。
    """
    keep = set(only) if only is not None else None
    return [d.schema() for n, d in REGISTRY.items()
            if (surface is None or surface in d.surfaces) and (keep is None or n in keep)]


def mcp_tools() -> list[ToolDef]:
    """对外暴露的工具，按注册顺序。gateway 用它构建 SPECS。"""
    return [d for d in REGISTRY.values() if "mcp" in d.surfaces]


def missing_handlers(impl, surface: str | None = None) -> list[str]:
    """注册表里有、但传入的实现集合里没有的工具名。

    方向是**单向**的：实现多于注册表不算错（可能有内部临时函数），
    注册表多于实现才是 bug——模型看得见却调不动，一次失败后就会乱试别的工具。
    """
    keep = [n for n in names(surface) if n not in impl]
    return keep


def orphan_handlers(impl, surface: str = "agent") -> list[str]:
    """实现里存在、但没进注册表的工具——这类工具永远不会被模型看到，属于死代码。

    单独列出来（而不是并进 missing_handlers）是因为两者修法相反：
    前者补实现，后者补注册或直接删掉实现。
    """
    return [n for n in impl if n not in REGISTRY]
