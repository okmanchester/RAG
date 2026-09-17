"""Reproduce the source-grounded trial v2 revision, preserving the entire v1 batch."""
import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag_baseline.benchmark.core import (Corpus, digest, file_hash, export_review, import_review,
    quotas, read_jsonl, validate_item, write_json, write_jsonl)
from rag_baseline.benchmark.quality import POLICY_VERSION, evidence_audit, named_document


def revise(corpus, originals):
    output = []
    by_id = {i["id"]: i for i in originals}

    def doc(term):
        matches = [d for d in corpus.documents.values() if term in d["source_path"]]
        if len(matches) > 1:
            matches = [d for d in matches if Path(d["source_path"]).name.startswith(term)]
        if len(matches) != 1:
            raise ValueError(f"Ambiguous document: {term}")
        return matches[0]["doc_id"]

    def group(term, *ids):
        did = doc(term)
        return [corpus.evidence(did, bid, corpus.blocks[did, bid]["text"]) for bid in ids]

    def short(term, bid, text):
        return [corpus.evidence(doc(term), bid, text)]

    def add(old_id, question, points, bundles, forms, scope, note,
            span="single_unit", difficulty="easy", rationale="一个完整证据单元中直接提取明确事实。",
            multi="", mappings=None, behavior="answer", reason="", reference=None, interpretations=None):
        old = by_id[old_id if old_id in by_id else "pilot_v2-" + old_id]
        i = {k: copy.deepcopy(old[k]) for k in ("category",)}
        i.update(id="trial_v2-" + old["id"], schema_version=2, user_input=question,
                 reference=reference or "；".join(p.rstrip("。；") for p in points) + "。",
                 answer_points=points, evidence_groups=[{"id": f"g{n+1}", "alternatives": [b]} for n,b in enumerate(bundles)],
                 answer_form=forms, evidence_span=span, expected_behavior=behavior,
                 difficulty=None if behavior != "answer" else difficulty, difficulty_reason=rationale,
                 scope_note=scope, multi_source_reason=multi, query_style="natural",
                 point_evidence=mappings if mappings is not None else [[f"g{n+1}" for n in range(len(bundles))] for _ in points],
                 boundary_reason=reason, clarifying_interpretations=interpretations or [],
                 review_status="pending", reviewer="", review_notes="", review_checks={}, duplicate_group="", pilot=True,
                 source_id=old["id"], source_candidate_sha256=digest(old), revision_note=note,
                 generation_targets={k: old.get(k) for k in ("question_type", "difficulty", "query_style", "expected_behavior")},
                 revision_provenance={"author": "agent", "human_approved": False, "method": "source-grounded revision", "policy": POLICY_VERSION},
                 corpus_search=copy.deepcopy(old.get("corpus_search", [])) if behavior != "answer" else [])
        if behavior != "answer":
            i["corpus_search_provenance"] = {"source_id": old["id"], "status": "historical searches; human recheck required"}
        i["query_style"] = "document" if named_document(question, corpus) else "natural"
        validate_item(i, corpus)
        i["evidence_audit"] = evidence_audit(i, corpus)
        output.append(i)
        return i

    add("0001", "个人信息保护执法检查时，履行保护职责的部门可以采取哪些措施？",
        ["询问有关当事人，调查与个人信息处理活动有关的情况。", "查阅、复制相关合同、记录、账簿以及其他有关资料。",
         "实施现场检查，对涉嫌违法的个人信息处理活动进行调查。", "检查与个人信息处理活动有关的设备、物品。",
         "对有证据证明用于违法个人信息处理活动的设备、物品，向本部门主要负责人书面报告并经批准，可以查封或者扣押。"],
        [group("个人信息保护法", *range(168,173))], ["fact", "condition"], "限定为个人信息保护执法检查可采取的措施，包括查封、扣押条件。",
        "补齐第六十三条第四项及证据，拆出审批条件。", difficulty="medium", rationale="完整枚举并保留查封、扣押的证据和审批条件。")
    add("0002", "园区网络要支持上万租户隔离时，VLAN和VxLAN的标识位宽有何差别，哪种更适合这一规模？",
        ["VLAN的标识位宽为12 bit。", "VxLAN的标识位宽为24 bit，可支持约1600万租户隔离。",
         "所给对比材料指出VLAN的隔离规模不能满足上万租户场景，VxLAN更适合该规模。"],
        [group("园区网络", 625,628,629,630)], ["comparison", "condition"], "比较隔离规模；不将4096编码空间解释成无保留的可分配VLAN数量。",
        "补齐表头和跨页行，删除实际可用4096个的无条件断言。", difficulty="medium", rationale="读取跨页对照表并结合租户规模选择。")
    add("0003", "使用财政性资金设立的科研机构和社会力量创办的科研机构，在资源开放共享方面分别有什么要求？",
        ["利用财政性资金设立的科研机构应当建立健全科学技术资源开放共享机制，促进资源有效利用。",
         "国家鼓励社会力量设立的科研机构在合理范围内实行科学技术资源开放共享。"],
        [group("科学技术进步法",137,138)], ["comparison", "condition"], "仅比较两类机构的资源开放共享义务强度，不概括全部运行制度。",
        "进一步收窄到同一资源共享问题，避免遗漏现代院所制度及拼接财政项目问项。", difficulty="medium", rationale="区分应当与鼓励以及合理范围限定。")
    add("0004", "工业互联网平台为企业的运营管理提供解决方案时，可以提供哪些服务？",
        ["面向企业管理需求，可提供运营监测、风险控制、科学决策等服务。"],
        [group("解决方案分类方法",87)], ["fact"], "只问运营管理场景中的服务类型。", "删除商业层通论，改为单一可回答任务，取消跨文档标签。")
    add("0006", "评估工业互联网平台的跨行业能力时，需要考察哪些覆盖范围和行业内能力？",
        ["考察平台覆盖的特定行业范围。", "考察各行业的设备连接能力；离散行业看行业设备，流程行业看工艺流程数据采集点。",
         "考察各行业部署的机理模型、微服务组件和工业APP。", "考察各行业覆盖的企业用户数量或行业企业比例。"],
        [group("平台评价方法.docx",50,51,52,53)], ["fact"], "只问评价维度，原文未给出具体门槛数值。", "移除无法回答的具体量化要求。")
    add("0007", "在人工智能技术研发和应用方面，国家重点研发计划与国家自然科学基金的支持重点有什么不同？",
        ["国家重点研发计划继续推进高性能计算等重点专项，加大对人工智能相关技术研发和应用的支持。",
         "国家自然科学基金加强对人工智能前沿领域交叉学科研究和自由探索的支持。"],
        [short("新一代人工智能",165,"国家重点研发计划") + short("新一代人工智能",166,
         "继续推进高性能计算等重点专项实施，加大对人工智能相关技术研 发和应用的支持；国家自然科学基金加强对人工智能前沿领域交叉 学科研究和自由探索的支持。")],
        ["comparison"], "比较两类科研资助安排的支持重点。", "补上前一块的主语，改标比较题。")
    add("0008", "传输结构化数据时，同步式动态标签与异步式的设定时间周期模式，在标签发送时机上有什么不同？",
        ["同步式将动态标签自动封装在应用层协议头部，与数据对象绑定并同步传输。",
         "异步式设定时间周期模式在该周期最后一条数据传输完毕后再发送标签。"],
        [group("标签标识技术",207,208,209), group("标签标识技术",221,223)], ["comparison", "procedure"],
        "只比较同步式与异步式时间周期模式，不将异步式全部模式概括为后发标签。", "删除大量示例配置细节，原子化评分点。",
        span="within_document", difficulty="medium", rationale="比较两个模式的发送时机，保留异步模式限定。",
        multi="同步式和时间周期异步式分别提供一侧传输时序，比较需要两侧。", mappings=[["g1"],["g2"]])
    add("0009", "消费者购买商品时，在安全保障、了解商品信息和自主选择方面，分别享有哪些权利？",
        ["享有人身、财产安全不受损害的权利，并可要求商品符合安全保障要求。",
         "有权知悉商品真实情况，并根据商品情况要求经营者提供价格、产地、性能、有效期限、售后服务等有关信息。",
         "有权自主选择经营者、商品品种或服务方式，自主决定购买与否，并进行比较、鉴别和挑选。"],
        [group("消费者权益保护法_2013",24,25),group("消费者权益保护法_2013",26,27),group("消费者权益保护法_2013",28,29,30)],
        ["fact"], "仅问安全、知情和自主选择三类购物权利，不声称枚举全部消费者权利。", "补齐缺失的前置权利与自主选择条款，删掉监管职责拼接。",
        span="within_document", difficulty="medium", rationale="按三个明确维度汇总相邻条款。",multi="同一次购买活动的三类权利分别由不同条款支持。",mappings=[["g1"],["g2"],["g3"]])
    add("0010", "联网工业企业的网络安全等级如何按分数划分？属于三类行业的规模以上企业通常定为哪一级？",
        ["企业分级采用计分方式，满分100分。", "评分大于等于80分为三级；大于等于60分且小于80分为二级；小于60分为一级。",
         "属于三类行业的规模以上联网工业企业原则上为三级。"],
        [group("网络安全分类分级",21,22,23,24)], ["condition"], "陈述分数区间和原则定级，不推断原文未规定的例外优先级。", "取消不能直接定级的过度推断和correct_premise标签。",
        difficulty="medium", rationale="区分分数边界并保留原则上这一限定。")
    add("0011", "在网络数据安全监管中，国家网信部门负责什么工作？", ["国家网信部门负责统筹协调网络数据安全和相关监管工作。"],
        [group("数据安全法_",23)], ["fact"], "仅问国家网信部门的职责。", "去掉依据法律履职这一非必要独立评分点。")
    add("0012", "测算工业互联网平台发展指数时，分类指数内部的采集项权重，与各分类指数之间的权重，分别怎样确定？",
        ["分类指数内部采用均等权重法确定各采集项权重。", "各分类指数权重宜根据实际需求，组织通过专家打分法设置。"],
        [group("平台监测分析",165,170)], ["comparison", "procedure"], "只问两层权重的确定方法，不考察损坏公式。", "移除错误公式及对应证据块，保留可读文本考点。",
        difficulty="medium", rationale="区分采集项与分类指数两个权重层次。")
    add("0013", "提供AI虚拟陪伴聊天服务时，用户交互数据应如何保护？涉及未成年人信息时，还应履行哪些同意和审计义务？",
        ["依法落实数据产权等制度，采取数据加密、访问控制等措施保护用户交互数据安全。", "除法律另有规定或者权利人明确同意外，不得向第三方提供用户交互数据。",
         "向用户提供交互数据复制、删除等选项。", "除法律、行政法规另有规定或者取得用户单独同意外，不得将属于用户敏感个人信息的交互数据用于模型训练。",
         "处理不满十四周岁未成年人个人信息，应取得父母或者其他监护人同意。", "按照国家有关规定，自行或者委托专业机构对处理未成年人个人信息的合规情况进行审计。"],
        [group("拟人化互动",46,47,48,49),group("拟人化互动",50,51)], ["condition", "procedure"],
        "限定用户交互数据保护及未成年人同意、审计；审计范围不局限于不满十四周岁。", "拆开年龄条件，删除省级监管部门职责问项。",
        span="within_document",difficulty="hard",rationale="综合交互数据保护与未成年人规则，需保留多种例外并区分年龄范围。",
        multi="同一聊天服务的数据处理任务，普通交互数据规则和未成年人附加规则均不可缺。",mappings=[["g1"]]*4+[["g2"]]*2)
    add("0014", "测评人工智能加速芯片的模型与数据加密能力时，应如何验证受硬件保护的根密钥可被使用？",
        ["验证能调用受硬件保护的根密钥，直接或间接对模型参数或用户数据等进行加解密。"],
        [group("加速芯片",246,247)], ["procedure"], "只考察模型与数据加密测评，不涉及车联网标准分类。", "替换无关跨文档拼接题。")
    add("0016", "通过公共网络传输数据时，通信加密协议的版本和密钥保存方式应满足哪些安全要求？",
        ["使用通信加密协议，并更新升级存在已知安全漏洞的版本。", "对密钥进行安全存储，不应将密钥在代码中硬编码或明文存储。"],
        [[copy.deepcopy(e) for g in by_id['pilot_v2-0016']['evidence_groups'] for e in g['alternatives'][0]]], ["condition"],
        "限定公共网络传输时的协议版本维护和密钥存储。", "去掉文件提示，不将原文列举的协议名称误写成当前安全版本推荐。")
    add("0017", "面向公众提供生成式AI写作服务时，在保护他人身心健康和人身权益方面，有哪些禁止性要求？",
        ["不得危害他人身心健康。", "不得侵害他人肖像权、名誉权、荣誉权、隐私权和个人信息权益。"],
        [group("生成式人工智能服务",16)], ["condition"], "仅问他人身心健康及列举的人身权益保护。", "减少题面照搬概括语，明确考点范围。")
    add("0019", "算法推荐服务和深度合成服务在配合监管部门监督检查时，有什么共同要求？算法推荐服务还明确要求保留什么记录？",
        ["两类服务均应依法配合监督检查，并提供必要的技术、数据等支持和协助。", "算法推荐服务提供者应依法留存网络日志。"],
        [group("算法推荐管理",51),group("深度合成管理",46)], ["comparison", "condition"], "只比较监督检查配合义务；不声称另一类服务没有留存日志义务。",
        "以同一监管配合任务比较两份文件，删除用户权益和法律责任拼接。",span="cross_document",difficulty="medium",
        rationale="提炼跨文档共同义务，并识别所引条款明示的记录要求。",multi="共同义务必须核对两类服务；日志要求由算法推荐条款提供。",mappings=[["g1","g2"],["g1"]])
    add("0020", "为工厂提供工业控制系统设计、建设、运维和评估服务的单位，是否属于工业控制系统信息安全防护要求的适用对象？",
        ["属于适用对象：从事工业控制系统规划、设计、建设、运维、评估的企事业单位适用相关防护要求。"],
        [copy.deepcopy(by_id['pilot_v2-0020']['evidence_groups'][0]['alternatives'][0])], ["condition"], "只判断所述服务单位是否适用。", "改标适用条件题。")
    add("0021", "推动人工智能终端产品发展时，具身智能与通信设备融合、终端产品布局有哪些推进措施？",
        ["加强具身智能与信息通信融合创新，推动具身智能与网联通信模组和设备适配验证。",
         "发展人工智能手机和电脑、智慧家庭设备、智能穿戴设备等产品，构建智能化融合化的人工智能终端产品体系。"],
        [group("“人工智能+信息通信”",28)], ["fact"], "仅问终端发展措施，不询问原文没有的适用条件。", "删掉第二主题及无证据条件问项。")
    add("0022", "企业采用工业互联网平台时，可以培育哪些应用模式，实施服务应覆盖哪些环节？",
        ["可培育协同设计、协同供应链管理、产品全生命周期管理、供应链金融等平台应用新模式。", "平台应用全流程方法体系包括咨询、实施、评估、培训、采信。"],
        [group("平台建设及推广",21)], ["fact", "procedure"], "限定平台应用模式及其全流程方法体系。", "删除设备管理独立主题；补供应链金融，按原文纠正为评估、培训、采信，不再写运行。")
    add("0023", "网络产品或服务提供者在产品或服务中设置恶意程序时，会受到哪些处理？拒不改正或造成危害后果时，罚款对象和范围有什么变化？",
        ["由有关主管部门责令改正，给予警告。", "拒不改正或者导致危害网络安全等后果的，处五万元以上五十万元以下罚款。",
         "对直接负责的主管人员处一万元以上十万元以下罚款。"],
        [group("网络安全法_",51,119,120)], ["condition"], "限定网络产品或服务提供者设置恶意程序，并区分一般处理与加重条件。", "补充适用对象及上游条款。",
        difficulty="medium",rationale="匹配适用对象并区分处罚触发条件与罚款对象。")
    add("0024", "企业准备共享一级、二级和三级工业数据时，各级数据分别有哪些开放限制？",
        ["一级数据：鼓励企业在做好数据管理的前提下适当共享。", "二级数据：在上述前提下适当共享，但只对确需获取该级数据的授权机构及相关人员开放。",
         "三级数据：原则上不共享；确需共享的，应严格控制知悉范围。"],
        [group("工业数据分类分级",26)], ["condition", "comparison"], "只比较数据共享限制，不混入事件处置。", "明确一级数据结论并收窄任务。",
        difficulty="medium",rationale="按级别区分授权和例外共享条件。")
    add("0025", "在利用人工智能提升治理能力方面，城市基础设施、城乡治理和公共资源交易有哪些具体应用方向？",
        ["推动市政基础设施智能化升级，探索面向新一代智能终端的城市规划、建设与治理，提升城市运行智能化水平。",
         "加快人工智能产品和服务向乡村延伸，推动城乡智能普惠并开展人工智能社会实验。",
         "安全稳妥有序推进人工智能在政务领域应用，打造精准识别需求、主动规划服务、全程智能办理的政务服务模式。", "加快人工智能在公共资源招标投标中的应用，提升交易服务和监管水平。"],
        [group("国务院关于深入实施",28)], ["fact"], "限定治理能力这一主题内列明的应用方向。", "删除应用发展环境这一独立主题，压缩非原子评分点。")
    add("0026", "在黄埔文冲船舶产业链协同优化案例中，平台PaaS层采用哪些集群技术，并提供什么资源管理能力？",
        ["案例的平台PaaS层主要基于Kubernetes和Docker虚拟化集群技术。", "具备资源调度能力。"],
        [group("体系架构",408,415,416)], ["fact"], "仅描述黄埔文冲船舶产业链案例，不泛化到所有工业互联网平台。", "补案例锚点和跨页断句，移除文件名称。")
    add("0027", "AI生成内容服务提供者办理算法备案、安全评估手续时，是否需要提供生成合成内容标识相关材料？",
        ["需要，服务提供者在履行算法备案、安全评估等手续时，应提供生成合成内容标识相关材料。"],
        [short("标识办法",38,"**第十二条** 服务提供者在履行算法备案、安全评估等手续时，应 当按照本办法提供生成合成内容标识相关材料，")],
        ["condition"], "只确认提交义务，不要求原文未列出的材料清单。", "删去生效日期和未问的共享义务，改为提交义务判断。")
    add("0028", "测评人工智能加速芯片的调试接口、串口鉴权功能时，测试人员应执行哪些步骤？",
        ["审查芯片研制单位给出的技术方案，明确调用芯片调试接口、串口是否存在鉴权机制。",
         "尝试绕过鉴权机制或暴力猜测接口鉴权信息，调用芯片调试接口和串口。"],
        [group("加速芯片",164,165)], ["procedure"], "限定接口鉴权测试步骤。", "去掉文件提示，改标流程题。")
    add("0029", "面向公众的生成式AI写作服务上线后，应如何保护用户输入信息和使用记录，并处理用户查阅、更正或删除个人信息的请求？",
        ["依法保护用户输入信息和使用记录，不得收集非必要个人信息。", "不得非法留存能够识别用户身份的输入信息和使用记录。",
         "不得非法向他人提供用户输入信息和使用记录。", "依法及时受理和处理个人关于查阅、复制、更正、补充、删除其个人信息等的请求。"],
        [group("生成式人工智能服务",35,36)], ["condition", "procedure"], "限定服务提供阶段的信息保护和个人请求处理，不问训练或标注。", "补第十一条前半段的三类禁止行为，移除过宽的训练标注问项。")
    add("0030", "开展工业互联网平台信息自动化采集时，平台企业与第三方公共服务平台应怎样配合完成数据准备、获取和检查？",
        ["平台企业采用自动化手段准备数据。", "平台企业将已采集的数据存储到中间缓存区。",
         "第三方公共服务平台访问缓存区，实现自动化数据抓取。", "公共服务平台验证审查数据，纠正缺漏项等基础性错误。"],
        [group("平台监测分析",141,142,143,144,145)], ["procedure"], "只考察自动化采集流程和双方分工。", "用正文流程题替换参考文献导航题。")
    add("pilot_v2_supplement-0001", "天津5G园区案例中，智能井盖和智能垃圾桶分别监测什么状态，状态异常时如何告警？",
        ["智能井盖监测开启状态和水位状态，状态变化时通过园区智能监控平台向井盖管理、施工相关人员发送警情。",
         "智能垃圾桶通过超声波监控满溢程度，超过限制高度时告警，并通过5G网络向平台报告垃圾箱位置和编号等信息。"],
        [group("园区网络",746,747),group("园区网络",750,752)], ["comparison"], "只描述天津园区两个具体监测应用，不泛化设备参数。", "删除前言定义及无关参数，把问题收窄为同一监测告警比较。",
        span="within_document",difficulty="medium",rationale="对比两个案例应用的监测量和告警方式。",multi="两个应用分别提供一侧状态与告警信息。",mappings=[["g1"],["g2"]])
    add("pilot_boundary_authored-0001", "有一则待核实的说法：云桥数据交易服务有限公司在2026年8月因未履行数据交易中介义务被罚。现有资料能确认这次实际罚款是多少元吗？",
        ["现有资料没有该具体个案的处罚决定，无法确认说法和实际罚款金额。", "不能将法定罚款区间当作个案实际罚款，也不能编造金额。"],
        [], ["fact"], "合成个案，仅测试能否识别语料未提供实际处罚结果；不预设事件真实。", "明确个案待核实；保留历史全库检索供人工重新核验。",
        span="none",behavior="abstain",reason="固定语料提供一般规则，未提供此合成个案处罚决定；全库检索只能辅助，须人工确认。",
        rationale="边界题不分普通难度。",mappings=[[],[]])
    add("pilot_boundary_authored-0002", "公司打算把客户个人信息交给另一家公司处理，需要取得个人的单独同意吗？",
        ["需要先澄清接收公司是受托处理，还是作为独立的个人信息处理者接收信息。",
         "受托处理与向其他个人信息处理者提供信息适用不同规则，不能在关系不明确时给出统一的是或否。"],
        [group("个人信息保护法",62),group("个人信息保护法",66)], ["condition"], "澄清处理关系，不将任一情形概括成无条件同意或无需同意。", "删除文件名，保持必要澄清并收敛答案。",
        span="within_document",behavior="clarify",reason="委托处理与向独立处理者提供信息两种合理解释会改变适用义务。",
        rationale="边界题不分普通难度。",multi="两项条款分别支持两种会改变义务的处理关系。",
        reference="请先明确：另一家公司只是按约定受托处理，还是作为独立的个人信息处理者接收这些信息？两种关系适用不同要求，未明确前不能给出统一的是或否；也不能仅凭受托关系就断言所有处理都无需同意。",
        interpretations=["按委托约定处理客户信息的受托方。","作为独立个人信息处理者接收客户信息的另一家公司。"])
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/benchmark/trial_v1"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/trial_v2"))
    parser.add_argument("--corpus", type=Path, default=Path("data/processed/baseline_v1"))
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Revision history is append-only; use a new output directory")
    corpus = Corpus(args.corpus)
    protected = [p for r in (Path("data/raw"),args.corpus,args.source) for p in r.rglob("*") if p.is_file()]
    protected += [p for p in Path("data/benchmark").glob("*graph*") if p.is_file()]
    before = {p.as_posix(): file_hash(p) for p in protected}
    originals = read_jsonl(args.source/"candidates.jsonl")
    rows = revise(corpus, originals)
    if len(rows) != 30 or sum(i["query_style"] == "document" for i in rows) > 3:
        raise ValueError("Trial size/style acceptance failed")
    manifest = json.loads((args.source/"manifest.json").read_text(encoding="utf-8"))
    write_jsonl(args.output/"candidates.jsonl", rows)
    write_json(args.output/"manifest.json", {"schema_version": 2, "status": "candidates_pending_human_review",
        "corpus": corpus.identity(), "judge": manifest["judge"], "evidence_policy": POLICY_VERSION,
        "source_batch": str(args.source), "source_manifest_sha256": file_hash(args.source/"manifest.json"),
        "implementation_sha256": {p.name:file_hash(p) for p in [Path(__file__),*Path('rag_baseline/benchmark').glob('*.py')]},
        "generator": {"kind":"agent_source_revision", "paid_api_calls":0}, "protected_files": before})
    write_json(args.output/"families.json", json.loads((args.source/"families.json").read_text(encoding="utf-8")))
    export_review(rows, args.output/"review", corpus)
    imported = import_review(rows,args.output/"review/review.csv",corpus)
    for a,b in zip(rows,imported):
        for key in ("user_input","reference","answer_points","evidence_groups","answer_form","difficulty","point_evidence"):
            if a[key] != b[key]:
                raise ValueError("CSV round trip mismatch: " + key)
    after = {p.as_posix():file_hash(p) for p in protected}
    if before != after:
        raise ValueError("Protected source changed")
    write_jsonl(args.output/"revisions.jsonl", [{"id":i["id"],"source_id":i["source_id"],"source_candidate_sha256":i["source_candidate_sha256"],
        "note":i["revision_note"],"changed_fields":[k for k in ("user_input","reference","answer_points","evidence_groups")
                                                if i[k] != next(o for o in originals if o['id']==i['source_id'])[k]]} for i in rows])
    write_json(args.output/"summary.json", {"candidate_count":len(rows),"approved":0,"quotas":quotas(rows),
        "csv_round_trip_verified":True,"protected_files_unchanged":True,"protected_file_count":len(before),
        "boundary_gap":"0010改为普通回答题；当前仅abstain/clarify各1题，后续需补有明确反证的correct_premise。",
        "review_required":"所有题目仍需人工核验语义、证据质量、重复题及边界全库搜索。"})
    (args.output/"README.md").write_text("# 修订试审集 v2\n\n审核入口：review/reading.md 与 review/review.csv。30题全部待人工审核。\n\n"
        "历史trial_v1、原始文档、解析快照及KG均未修改。revisions.jsonl记录逐题来源与修改，review/evidence_audit.jsonl记录原文坐标和质量提示。\n\n"
        "当前28道普通题、2道边界题；correct_premise缺口保留，不通过错标补齐。该试审集及近重复题不能进入holdout。\n",encoding="utf-8")
    print(json.dumps({"output":str(args.output),"candidates":len(rows),"protected_files_unchanged":True}))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
