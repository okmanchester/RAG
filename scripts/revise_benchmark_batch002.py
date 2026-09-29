"""Revise the 42 flagged batch_002 items against fixed sources; append only.

Run in conda rag. No model API calls, corpus changes, freeze or split assignment.
"""
import argparse
import copy
import csv
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rag_baseline.benchmark.core import (Corpus, REVIEW_CHECKS, digest, duplicate_clusters,
    export_review, file_hash, import_review, now, read_jsonl, validate_item, write_json, write_jsonl)
from rag_baseline.benchmark.quality import (CONTENT_CHECKS, content_fingerprint,
    evidence_audit, named_document)

SOURCE = ROOT / 'data/benchmark/batch_002_review64'
AUTH = '帮我修订这些需要修订的42题，已由我人工授权'
REVIEWER = 'Codex（用户授权原文修订及自检；非独立人工复审）'


def revise(corpus, originals):
    items = copy.deepcopy(originals)
    notes = {}

    def doc(term):
        docs = [d for d in corpus.documents.values() if term in Path(d['source_path']).name]
        if len(docs) != 1:
            raise ValueError(f'Ambiguous source: {term}: {[d["source_path"] for d in docs]}')
        return docs[0]['doc_id']

    def b(term, *ids):
        did = doc(term)
        return [corpus.evidence(did, bid, corpus.blocks[did, bid]['text']) for bid in ids]

    def change(n, note, question=None, points=None, bundles=None, forms=None,
               span='single_unit', difficulty='easy', scope=None, multi='', mappings=None,
               behavior='answer', reference=None, category=None, boundary_reason=''):
        item = items[n-1]
        if question is not None:
            item['user_input'] = question
        if points is not None:
            item['answer_points'] = points
        item['reference'] = reference if reference is not None else '；'.join(p.rstrip('。；') for p in item['answer_points']) + '。'
        if bundles is None:
            raise ValueError(f'Explicit source bundles required: {n}')
        item['evidence_groups'] = [{'id':f'g{k+1}', 'alternatives':[bundle]} for k,bundle in enumerate(bundles)]
        item.update(answer_form=forms or ['fact'], evidence_span=span,
            difficulty=difficulty if behavior == 'answer' else None,
            expected_behavior=behavior, query_style='document' if named_document(item['user_input'],corpus) else 'natural',
            scope_note=scope or item['user_input'], multi_source_reason=multi,
            difficulty_reason=('边界行为题，不设置普通题难度。' if behavior != 'answer' else
                ('需按题面指定维度筛选、整理同一完整表格或条款的多项信息。' if difficulty=='medium' and span=='single_unit' else
                 '从一个完整条款或连续枚举中直接提取给定范围的要求。' if span=='single_unit' else
                 '需结合多个不可替代的证据单元，完成同一具体任务。')),
            boundary_reason=boundary_reason, clarifying_interpretations=[], corpus_search=[])
        if category:
            item['category'] = category
        item['point_evidence'] = mappings if mappings is not None else [['g1'] for _ in item['answer_points']]
        if mappings is None and len(bundles) != 1:
            raise ValueError(f'Explicit point mapping required: {n}')
        notes[n] = note

    change(3, '补运营者主体及第55条起点；补事件分级和相应应急措施，按主体和动作拆点。',
        question='关键信息基础设施运营者在应急预案制定和演练方面有什么义务？负责其安全保护的部门在监测预警、通报、预案制定演练方面有什么职责？预案对事件分级和处置措施应怎样规定？',
        points=['运营者应制定网络安全事件应急预案','运营者应定期进行演练',
                '保护部门应建立健全本行业、本领域网络安全监测预警和信息通报制度',
                '保护部门应按规定报送网络安全监测预警信息',
                '保护部门应制定本行业、本领域网络安全事件应急预案','保护部门应定期组织演练',
                '预案应按事件发生后的危害程度、影响范围等因素对网络安全事件分级',
                '预案应规定相应的应急处置措施'],
        bundles=[b('网络安全法_',69,73),b('网络安全法_',103),b('网络安全法_',104,105,106)],
        forms=['comparison','fact'], span='within_document', difficulty='medium',
        mappings=[['g1']]*2+[['g2']]*2+[['g3']]*4,
        multi='围绕同一关键信息基础设施应急工作，第36条给运营者义务，第54条给保护部门监测通报，第55条给部门预案与事件分级要求，各条不能代替其他条。')
    change(4, '去除已由旧题覆盖的芯片加密半题及导航指标；聚焦平台数据加密存储，保留可/建议/应的力度。',
        question='工业互联网平台存储数据时，可怎样按敏感度选择加密措施？密码设施和密钥应按什么规定管理？若客户在平台外先加密再上传存储，对平台运营商和第三方解密有什么约束？',
        points=['可按数据敏感度采用分等级加密存储，如不加密、部分加密、完全加密',
                '建议按国家密码管理有关规定使用和管理密码设施',
                '建议按规定生成、使用和管理密钥',
                '平台外加密后上传存储的客户数据，应确保平台运营商或任何第三方无法解密'],
        bundles=[b('体系架构（版本2.0）',343)],forms=['condition','fact'],
        scope='只考加密存储及平台外预先加密情形，不把其扩成全部传输、备份要求；不再考旧题的芯片根密钥测评。')
    change(7, '不再给题面已明确的身份和工作事实计分，只考优先待遇和国家规定限定。',
        question='外籍杰出科学技术人员到中国从事科学技术研究开发，在居留或国籍方面可以享受什么优先待遇？这是否意味着可以不受国家有关规定限制而自动取得身份？',
        points=['可以优先获得在华永久居留权或者取得中国国籍',
                '须按照国家有关规定，原文的“可以优先”不表示无条件、自动取得身份'],
        bundles=[b('科学技术进步法_',190)], forms=['condition','fact'])
    change(8, '补表头及跨行设备指标全称，明确月为更新频率而非擅自推成月内新增量；补分类和区域统计口径。',
        question='工业互联网平台监测分析中，一级指标“平台数据总量”和“平台工业设备连接数量”的口径、单位和更新频率是什么？设备连接数量下面的分类统计和按区域统计分别如何统计？',
        points=['平台数据总量是平台数据资源存储量，包括应用、用户、过程和系统实际存储等支撑平台及应用运行的数据',
                '数据总量包括所有应用系统使用的云存储量及非云存储量总和',
                '平台数据总量的单位为TB，更新频率为月',
                '平台工业设备连接数量基于平台连接的工业设备，包括公有云、私有云、专有网连接设备',
                '平台工业设备连接数量的单位为台，更新频率为月',
                '分类统计参照附录A的工业设备二级分类统计数量',
                '区域统计按省级区域、计划单列市及海外地区等分别统计设备所属地连接数量',
                '设备所属地指设备所属企业注册地',
                '分类统计和按区域统计的单位均为台，更新频率均为月'],
        bundles=[b('平台监测分析指南',92,95,96,97)],difficulty='medium',
        scope='只问指定一级指标及设备的两个二级统计维度，不包括另一个指标公有云工业设备连接数量；表列月为更新频率，不断言是当月新增值。')
    change(9, '删除不对称宏观规划比较，限定人工智能+行动的智能算力统筹，拆独立措施。',
        question='“人工智能+”行动在智能算力统筹方面，对芯片和软件、集群与资源布局、互联供需及云服务提出了哪些措施？',
        points=['支持人工智能芯片攻坚创新','支持使能软件生态培育',
                '加快超大规模智算集群技术突破和工程落地','优化国家智算资源布局',
                '完善全国一体化算力网，发挥东数西算国家枢纽作用','加大数、算、电、网等资源协同',
                '加强智能算力互联互通和供需匹配','创新智能算力基础设施运营模式',
                '鼓励发展标准化、可扩展的算力云服务',
                '推动智能算力供给普惠易用、经济高效、绿色安全'],
        bundles=[b('国务院关于深入实施',39)],scope='只考智能算力统筹，不作为基础支撑全部八类措施或现实落实情况。')
    change(10, '保留真实前提纠错；补大二层主体及完整适用场景列表，连续枚举按single_unit。',
        points=['“只适合小型园区、不适合多园区及海量物联”的说法不对',
                '大二层园区网络的适用场景列有多园区统一组网',
                '适用场景还列有海量物联设备安全纳管'],
        bundles=[b('园区网络白皮书',335,336,337,338,339,340,341,342)],behavior='correct_premise',
        forms=['condition'],boundary_reason='题面否定多园区、海量物联适用性；同一大二层适用场景清单明确包含这两项，构成直接反证。不能据此保证任意规模、任意项目配置都满足性能指标。')
    change(11, '题面限定产业就业影响研判与技术风险体系；拆效果与体系要点，避免泛称全部AI安全措施。',
        question='人工智能应用带来的产业结构和就业结构影响应怎样研判、应对？为防范技术相关风险，应建立健全哪些监测、预警和响应体系？',
        points=['持续加强对产业结构和就业结构等影响的研判评估和应对',
                '增强就业创造效应','减缓就业替代效应',
                '建立健全人工智能技术监测体系','建立健全风险预警体系','建立健全应急响应体系'],
        bundles=[b('国家发展改革委有关负责同志就',32)],scope='只考所述产业就业影响研判及技术监测预警响应，不作为所有AI风险防范措施的清单。')
    change(12, '国际合作是措施事实，condition改fact；保留正确内容与完整小节。',
        bundles=[b('车联网网络安全和数据安全标准体系建设指南',78)], forms=['fact'])
    change(14, '删除平台推广目标与企业分类分级无必要拼接；聚焦二、三级安全防护比较，保留征求意见稿身份。',
        question='按固定材料《工业互联网企业网络安全分类分级指南（试行）》（征求意见稿），二级和三级企业在技术防护、安全监测平台建设与接入方面分别有什么要求？风险通报涉及哪个级别？',
        points=['三级企业应按工业互联网网络安全管理和技术防护系列标准规范采取相应技术防护措施',
                '三级企业应建设完善企业级安全监测平台','三级企业平台应接入省级以上安全监测平台',
                '省级以上安全监测平台定期向三级企业通报安全风险',
                '二级企业应按相关管理和技术防护系列标准规范采取相应技术防护措施',
                '二级企业应积极建设企业级安全监测平台','二级企业平台应与省级安全监测平台对接'],
        bundles=[b('网络安全分类分级管理指南',2,43,44,45)],forms=['comparison'],difficulty='medium',
        scope='只适用本库明确标征求意见稿的第（二）安全防护段，不冒充现行最终分级规则；不问所有适用主体或平台建设推广目标。')
    change(15, '明确16岁和虚拟伴侣，无澄清歧义，正常回答禁止提供；纠正原审核将第21号令误称草案的说明。',
        question='AI陪伴应用只向16岁高中生提供虚拟伴侣这一虚拟亲密关系服务，是否允许？',
        points=['不允许：拟人化互动服务提供者不得向未成年人提供虚拟亲属、虚拟伴侣等虚拟亲密关系服务'],
        bundles=[b('拟人化互动服务管理暂行办法__',42)], forms=['condition'],category='policy',
        scope='题面已明确未成年年龄及虚拟伴侣服务，仅适用本库第21号令第14条第一分句；不满14周岁其他服务的同意规则不用于本场景。')
    change(16, '取消无日期限定的当前全称判断，以分类方法引言描述为范围；保留问题与后果的因果对应。',
        question='《工业互联网平台解决方案分类方法》的引言认为，哪些平台互通和企业落地问题限制了解决方案复制推广？这些问题造成了什么后果？',
        points=['平台各自为政、互联互通困难','工业企业开展平台应用与数字化建设无路可问、无章可循',
                '导致解决方案复制推广受限，制约平台经济性和规模化推广'],
        bundles=[b('平台解决方案分类方法',36)],scope='回答固定标准引言中的问题描述，不外推为2026年全部平台的现实现状。')
    change(17, '删旧题已覆盖的通信协议、密钥保存，保留跨安全域及大量个人信息跨境；补5.5起点，连续枚举合并。',
        question='数据库通过公共网络传输数据，若跨不同安全域，应怎样建立链路和处理输出数据？涉及大量个人信息跨境传输的数据库还须满足什么额外要求？',
        points=['跨不同安全域应建立安全或专用传输链路','应根据业务实际情况对输出数据实施脱敏处理',
                '涉及大量个人信息跨境传输的数据库，还应满足国家关于数据出境安全评估的相关要求'],
        bundles=[b('数据库联网安全要求',109,112,113,114)],forms=['condition'],
        scope='只问网络传输5.5的c/d情形，不给数据量阈值或具体评估流程；a/b已由旧题覆盖。')
    change(18, '删定义冒充标识义务及过宽内容清单，限定防歧视和服务透明、内容准确可靠；与本批0047的标识任务分开。',
        question='提供生成式人工智能服务时，在算法设计、训练数据选择、模型生成优化和提供服务过程中，防止歧视有什么要求？服务透明度、生成内容的准确性与可靠性又应怎样提升？',
        points=['在算法设计、训练数据选择、模型生成和优化、提供服务等过程中采取有效措施，防止民族、信仰、国别、地域、性别、年龄、职业、健康等歧视',
                '基于服务类型特点采取有效措施提升服务透明度','提高生成内容的准确性','提高生成内容的可靠性'],
        bundles=[b('生成式人工智能服务管理暂行办法__',11,13,16)],scope='只问第4条第2、5项，不作为第4条全部义务或生成内容标识的回答。')
    mandatory = [('经营能力','运营能力'),('经营能力','盈利能力'),('创新能力','创新投入水平'),('创新能力','创新产出水平'),
                 ('技术能力','应用开发水平'),('技术能力','数据服务水平'),('技术能力','新技术融合水平'),
                 ('服务成效','经济效益'),('服务成效','行业影响力'),('生态能力','生态合作范围')]
    optional = [('经营能力','商业模式'),('经营能力','发展空间'),('创新能力','持续改进能力'),
                ('技术能力','资源汇聚水平'),('技术能力','安全防护水平'),('服务成效','社会效益'),
                ('生态能力','开放兼容水平'),('生态能力','融合服务水平'),('生态能力','生态价值转化')]
    change(21, '补附录A表头和续页，按五个一级指标下的完整19个二级指标逐项计分；题面只问选择属性，不再混列采集项。',
        question='《工业互联网平台服务商评价方法》附录A示例表中，五个一级指标下的哪些二级指标标为必选、哪些标为可选？该表是资料性示例还是规范性强制清单？',
        points=[f'{parent}下的{name}标为必选' for parent,name in mandatory]+
               [f'{parent}下的{name}标为可选' for parent,name in optional]+['附录A是资料性示例表，不将示例表外推为一切评价的规范性强制清单'],
        bundles=[b('平台服务商评价方法',*range(154,194))],difficulty='medium',
        scope='只考附录A完整19个二级指标的必选/可选属性和资料性身份，不问采集项、权重或实际评价是否全套采用。')
    change(23, '删无原文支持的人机物机制桥接与重复总括分，保留完整四类资源能力并拆动作；一个连续枚举单元。',
        question='工业互联网平台资源管理能力评价中，工业设备、软件应用、用户与开发者、数据资源四方面分别有哪些具体要求？',
        points=['工业设备管理：兼容多类工业通信协议，采集生产装备、装置和工业产品数据',
                '工业设备管理：部署终端边缘计算模块，实现设备数据实时处理',
                '工业设备管理：适配主流控制系统，实现参数配置、功能设定和维护管理',
                '软件应用管理：可按云服务架构提供研发、采购、生产、营销、管理和服务等工业软件',
                '软件应用管理：提供工业软件集成适配接口',
                '软件应用管理：可按平台即服务架构提供工业场景的机理模型、微服务组件和工业APP',
                '软件应用管理：具备软件及工业APP的搜索、认证、交易、运行和维护管理能力',
                '用户管理：具备多租户权限管理','用户管理：具备用户需求响应','用户管理：具备交易支付等功能',
                '开发者管理：建开发者社区并集聚开发者','开发者管理：提供应用开发、测试、部署和发布的服务管理功能',
                '数据资源管理：具备海量工业数据存储管理','数据资源管理：部署多类结构化、非结构化数据管理系统',
                '数据资源管理：提供存储、编目、索引、去重、合并及质量评估等功能'],
        bundles=[b('工业互联网平台评价方法',6,7,8,9,10)])
    change(24, '分类分级原则为真，correct_premise改abstain；拒答细则缺失不设伪必要证据组，正向原则正文及解读留在全库检索审计。',
        question='我们开发面向公众的生成式人工智能聊天服务。按现有材料中的分类分级监管规则，这项服务应归入哪一级，各项具体分级要求是什么？',
        points=['无法据固定材料确定该服务的具体监管级别','无法据固定材料列出该级别的具体分级要求'],
        bundles=[],span='none',forms=['fact'],behavior='abstain',
        mappings=[[],[]],
        reference='固定材料规定有关主管部门制定相应分类分级监管规则或指引，但全库未给生成式人工智能服务的具体等级划分细则，也未给此聊天服务的等级归属及逐级要求。因此无法确定所问级别和具体分级要求，不能把工业企业或工业数据的等级规则套用过来。',
        boundary_reason='全库扫描与命中上下文核对，区分生成式AI监管原则与工业企业/数据等级；固定库没有所问服务的具体分级细则，不能给级别或要求。只陈述固定材料覆盖，不推断现实不存在相关规则。')
    change(26, '两款同属数据安全法第53条，合为单一证据单元；保留适用规则。',
        bundles=[b('数据安全法_',88,89)],forms=['condition'])
    change(27, '删除无关煤矿语音传输；补AC备份表头及适用范围，逐方式拆切换、业务影响和部署条件。',
        question='工业园区无线网络选择VRRP双机热备、双链路双机热备或N+1备份时，三者在切换、业务影响、主备AC异地部署和可靠性成本适用场景上有什么差异？',
        points=['VRRP主备切换快，可通过配置抢占时间实现更快切换','VRRP对业务影响小',
                'VRRP为二层协议，不支持主备AC异地部署','VRRP适用可靠性要求高且无须主备AC异地部署的场景',
                '双链路双机热备的AP切换慢，等CAPWAP断链超时后才切换','双链路主备切换后终端无需重新上线',
                '双链路支持主备AC异地部署','双链路适用可靠性要求高且需异地部署主备AC的场景',
                'N+1的AP切换慢，等CAPWAP断链超时后才切换','N+1的AP和终端都需重新上线，业务短暂中断',
                'N+1支持主备AC异地部署','N+1适用可靠性要求较低、成本控制要求较高的场景'],
        bundles=[b('园区网络白皮书',383,385,386,387)],forms=['comparison'],difficulty='medium')
    change(29, '撤销虚假澄清，直接回答经济效益关键指标；独立指标分开评分。',
        points=['助力企业提高收入的优化情况','提高效率的优化情况','降本减损的优化情况',
                '提升产品和服务质量的优化情况','促进企业模式创新的情况','提高企业竞争力的情况'],
        bundles=[b('平台服务商评价方法',97,98)],scope='只问服务成效下经济效益，不作为全部服务成效二级指标，原文等字保留非穷举性质。',
        reference='经济效益考察助力企业提高收入、提高效率、降本减损、提升产品和服务质量等关键指标的优化情况，以及促进模式创新、提高竞争力等情况。')
    change(32, '删旧题个人信息半题和过宽用户权益清单，限定违法内容、违法使用与投诉举报处置；第14条两款合并，拆独立动作。',
        question='生成式人工智能服务提供者发现违法内容，或发现使用者利用服务从事违法活动时，分别应如何处置、整改或留存报告？公众投诉举报机制应怎样设置和运行？',
        points=['发现违法内容应及时停止生成、停止传输、消除等处置','发现违法内容应采取模型优化训练等措施整改',
                '发现违法内容应向有关主管部门报告',
                '发现用户从事违法活动应依法依约警示、限制功能、暂停或者终止提供服务等处置',
                '用户违法活动处置应保存有关记录','用户违法活动处置应向有关主管部门报告',
                '建立健全投诉举报机制','设置便捷的投诉举报入口','公布处理流程','公布反馈时限',
                '及时受理处理公众投诉举报','反馈处理结果'],
        bundles=[b('生成式人工智能服务管理暂行办法_国务院部门',39,40),
                 b('生成式人工智能服务管理暂行办法_国务院部门',41)],
        forms=['condition','procedure'],span='within_document',difficulty='medium',
        mappings=[['g1']]*6+[['g2']]*6,
        multi='同一服务违规处置和公众救济任务：第14条规定两种违规触发的处置留存报告，第15条规定投诉举报流程，二者各有独立必需内容。')
    change(33, '删除无关工业安全文件比较，只考信息通信融合到2028/2030的目标；来源单一政策，category改policy。',
        question='《“人工智能+信息通信”创新发展实施意见》对到2028年和到2030年的融合发展目标分别怎样安排？2028年的典型场景数量和城域算力时延圈覆盖率目标是什么？',
        points=['到2028年初步构建人工智能与信息通信融合互促的创新发展格局',
                '2028年信息通信智能运营和服务能力达到国际先进水平','2028年信息通信网络初步实现高等级自智',
                '2028年形成30个以上高价值典型场景','2028年打造一批典型应用和特色智能体',
                '2028年网络、算力等信息基础设施支撑AI能力进一步提升',
                '2028年城域算力1毫秒时延圈覆盖率不低于75%',
                '到2030年人工智能与信息通信网络融合关键核心技术取得显著突破','2030年通感算智一体化服务能力大幅提升',
                '2030年形成完备协同创新和产业生态体系','2030年步入技术引领、产业繁荣、安全可靠、智能普惠的新阶段'],
        bundles=[b('“人工智能+信息通信”创新发展实施意见.pdf',5)],forms=['comparison','fact'],difficulty='medium',category='policy',
        scope='只描述固定实施意见的目标，不将目标当成已经实现的结果。')
    change(34, '删除待制定标准元数据证据，保留具体审批流程时限拒答；全库检索包含一般出境条件，并显式区别具体操作细则。',
        question='我们开发智能客服系统，想在深入实施“人工智能+”行动中办理企业数据出境审批。现有材料给出的具体办理流程和审批时限是什么？',
        points=['不能从固定材料确定这家企业数据出境审批的具体办理流程',
                '不能从固定材料给出其审批时限，不应编造办理步骤或天数'],
        bundles=[],span='none',forms=['fact'],behavior='abstain',mappings=[[],[]],
        reference='固定库有个人信息保护法第38—40条等一般出境条件，数据库联网指南也有事先安全评估、依法报批要求，但没有给“人工智能+”行动下适用于这家智能客服企业的具体审批办理流程和时限。不能把一般条件当作完整办理流程，也不能编造审批天数。这里只说明当前固定库的覆盖范围，不代表现实没有相关规定。',
        boundary_reason='逐份扫描全库并阅读一般出境条款与办理期限命中；一般评估、认证、标准合同、告知同意、报批要求不提供所问企业的具体办理流程和审批时限，其他备案或消费调解期限不能移用。')
    change(35, '以5.4实文替换纯章节跳转，补6.4完整操作及结果；技术指标与测试方法为同一测评任务的两个必要单元。',
        question='测评人工智能加速芯片安全存储单元时，实际技术指标是什么？应执行哪些检查、尝试，预期结果是什么？',
        points=['应提供片内安全存储单元，如一次性烧写、仅授权进程读取',
                '应保护密钥等敏感参数的机密性','应保护密钥等敏感参数的完整性',
                '审查研制单位技术方案，明确是否为密钥等敏感参数提供安全存储单元',
                '尝试篡改密钥信息','尝试非法访问密钥信息',
                '预期为敏感参数提供安全存储单元','预期无法非授权篡改其中密钥信息','预期无法非授权访问其中密钥信息'],
        bundles=[b('加速芯片安全功能技术规范',96),b('加速芯片安全功能技术规范',216,219,220,221,222)],
        forms=['procedure','fact'],span='within_document',difficulty='medium',
        mappings=[['g1']]*3+[['g2']]*6,
        multi='5.4规定实际存储安全指标，6.4规定验证方法和结果；同一测评任务必须同时知道满足什么和怎样测试，不能只用编号跳转作答案。')
    change(36, '删除未问的标准协商主体背景计分；三条解决途径单独评分。',
        points=['协商不成可以向国家著作权主管部门申请裁决','对裁决不服可以向人民法院提起诉讼',
                '也可以直接向人民法院提起诉讼'],bundles=[b('著作权法_',42)],forms=['procedure','condition'])
    change(37, '删除与异步规则无必要关系的静态模板；补d项完整批量打标与约定粒度，保留应/宜。',
        question='结构化数据采用异步式动态标签打标时，发送方应怎样生成绑定并传送数据对象和标签？对频繁传输的同结构单条数据，批量打标和粒度约定有什么要求？',
        points=['发送方应在数据对象传输前生成动态标签标识','应完成标签与数据对象的逻辑绑定',
                '应按与接收方约定的方式分别传输对象和标签，可包括不同API接口或不同顺序等',
                '应通过接口功能实现动态标签生成和网络数据打标流程',
                '频繁多次传输同结构单条数据时，宜对批量数据统一打标',
                '可通过设定时间周期或设定计数器两种方式实现批量打标',
                '周期和计数器可按实际需要，由发送方和接收方约定适当粒度'],
        bundles=[b('网络数据标签标识技术要求',215,216,217,220,221)],forms=['procedure','condition'])
    change(38, '取消未指定两份文件的比较和农业旧覆盖交集，明确2017发展规划的智能制造任务并补全文。',
        question='《新一代人工智能发展规划》推进智能制造时，在装备软件集成、智能产品和云平台、新型制造模式、标准及全生命周期方面提出了哪些措施？',
        points=['推进智能制造关键技术装备、核心支撑软件、工业互联网等系统集成应用',
                '研发智能产品及智能互联产品','研发智能制造使能工具与系统','研发智能制造云服务平台',
                '推广流程智能制造','推广离散智能制造','推广网络化协同制造','推广远程诊断与运维服务等新模式',
                '建立智能制造标准体系','推进制造全生命周期活动智能化'],
        bundles=[b('新一代人工智能发展规划的通知',115)],scope='只问固定规划智能制造段，不声称涵盖所有产业智能化方向或当前落实状态。')
    change(39, '完整保留第七节并纠错，除题面已有设备和预案外，补及时发现报告处理、深包防护、事件响应及演练修订；连续清单single_unit。',
        points=['仅部署监测设备和写好预案不足以完全符合该节要求',
                '应及时发现网络攻击或异常行为','应及时报告网络攻击或异常行为','应及时处理网络攻击或异常行为',
                '应在重要工控设备前端部署具备工业协议深度包检测功能的防护设备，限制违法操作',
                '遇安全威胁导致异常或故障，应立即紧急防护以防止事态扩大',
                '事件应逐级报送直至属地省级工业和信息化主管部门','应注意保护现场以便调查取证',
                '应定期演练工控应急响应预案','必要时修订应急响应预案'],
        bundles=[b('工业控制系统信息安全防护指南',39,40,41,42,43,44)],forms=['condition','procedure'],behavior='correct_premise',
        boundary_reason='题面把两项准备工作等同于完整合规；第七节还要求及时发现报告处理、深包防护、特定事件紧急响应报送取证、演练修订，形成明确反证。不将题面未述情况推为该公司实际已违反全部义务。')
    change(42, '保持三种触发场景，补第10/21条主体起点并拆动作；不把可以要求暂停误写为必然全停。',
        points=['发现违法和不良信息应依法处置','发现违法和不良信息应保存有关记录',
                '发现违法和不良信息应及时向网信部门和有关主管部门报告',
                '对相关使用者依法依约警示、限制功能、暂停服务、关闭账号等处置',
                '发现利用服务制作复制发布传播虚假信息应及时辟谣','该虚假信息场景应保存有关记录',
                '该虚假信息场景应向网信部门和有关主管部门报告',
                '较大信息安全风险时，主管部门可按职责依法要求提供者及技术支持者暂停信息更新、用户账号注册或者其他相关服务',
                '提供者和技术支持者应按要求采取措施','应进行整改','应消除隐患'],
        bundles=[b('互联网信息服务深度合成管理规定',19,21),b('互联网信息服务深度合成管理规定',22),
                 b('互联网信息服务深度合成管理规定',46,47)],forms=['condition','procedure'],span='within_document',difficulty='medium',
        mappings=[['g1']]*4+[['g2']]*3+[['g3']]*4,
        multi='同一深度合成风险处置任务需区别第10条违法不良信息、第11条虚假信息和第21条主管部门风险处置，三种触发条件和义务不互相替代。')
    change(43, '补工业数据各级抗攻击保护要求，数据安全法完整第21条合并，不重复总括打分。',
        question='企业制定工业数据分级保护方案时，工业数据分类分级指南的三级划分依据和各级抗攻击要求是什么？数据安全法对通用分类分级、重要数据和国家核心数据规定了什么保护依据和要求？',
        points=['工业数据按被篡改、破坏、泄露或非法利用后对工业生产、经济效益等的潜在影响分一级、二级、三级',
                '三级数据措施应能抵御国家级敌对组织的大规模恶意攻击','二级数据措施应能抵御大规模、较强恶意攻击',
                '一级数据措施应能抵御一般恶意攻击',
                '数据安全法分类分级依据包括数据在经济社会发展中的重要程度',
                '还包括数据遭篡改破坏泄露或非法获取利用对国家安全、公共利益或个人组织合法权益的危害程度',
                '国家数据安全协调机制统筹协调有关部门制定重要数据目录并加强保护',
                '关系国家安全、国民经济命脉、重要民生、重大公共利益等数据属国家核心数据，实行更加严格管理',
                '各地区各部门确定本地区、本部门及行业领域的重要数据具体目录，对列入数据重点保护'],
        bundles=[b('工业数据分类分级指南',12),b('工业数据分类分级指南',24,25),b('数据安全法_',43,44,45)],
        forms=['comparison','condition'],span='cross_document',difficulty='medium',
        mappings=[['g1']]+[['g2']]*3+[['g3']]*5,
        multi='同一企业工业数据保护方案对照专门指南的三级潜在影响和抗攻击能力，与数据安全法通用重要/核心数据制度；不能把工业三级直接等同国家核心数据，一份来源不覆盖另一制度。',
        scope='只比较两份固定材料明确规定，不判断某条实际数据的级别或目录归属，不等同工业三级与国家核心数据。')
    change(45, '删除旧题跨行业和存储计算半题；聚焦其余四类应用服务，按独立功能拆要点。',
        question='评价工业互联网平台应用服务能力时，除存储计算外，应用开发、平台间调用、安全防护和新技术应用四类服务分别有什么要求？',
        points=['应用开发：提供多类开发语言、开发框架和开发工具','应用开发：提供通用建模分析算法',
                '应用开发：支撑数据模型及软件应用快速开发，满足多行业多场景需求',
                '平台间调用：支持工业数据在不同IaaS平台自由迁移',
                '平台间调用：支持工业软件、机理模型、微服务、工业APP在不同PaaS平台部署、调用和订阅',
                '安全防护：部署安全防护功能模块或组件','安全防护：建立安全防护机制','安全防护：确保平台数据与应用安全',
                '新技术应用：具备新技术应用探索能力','新技术应用：开展人工智能、区块链、VR/AR/MR等技术应用'],
        bundles=[b('工业互联网平台评价方法',11,13,14,15,16)])
    change(46, '删除无关国家监管宏观清单，限定第4条适用服务且未申请无显式标识例外；显式第4条合并，隐式第5条另组，区分应/可以/鼓励。',
        question='《人工智能生成合成内容标识办法》中，服务属于深度合成规定第十七条第一款情形，且用户未申请不含显式标识内容时，文本、音频、图片、视频、虚拟场景等怎样添加显式标识，下载复制导出应怎样保留？文件元数据隐式标识与数字水印各有什么要求？',
        points=['文本在起始、末尾或中间适当位置加文字或通用符号提示，或者在交互界面、文字周边加显著提示',
                '音频在起始、末尾或中间适当位置加语音或音频节奏提示，或者在交互界面加显著提示',
                '图片在适当位置加显著提示','视频应在起始画面和播放周边适当位置加显著提示',
                '视频可以在末尾和中间适当位置加显著提示','虚拟场景应在起始画面适当位置加显著提示',
                '虚拟场景可以在持续服务过程中适当位置加显著提示','其他生成合成场景按自身特点加显著提示',
                '提供下载、复制、导出等功能时，文件中应含符合要求的显式标识',
                '应在文件元数据中添加隐式标识','隐式标识应含生成合成内容属性信息',
                '隐式标识应含服务提供者名称或编码','隐式标识应含内容编号等制作要素信息',
                '数字水印等形式的隐式标识属于鼓励添加，不将鼓励改写为强制'],
        bundles=[b('生成合成内容标识办法',*range(14,22)),b('生成合成内容标识办法',22,23)],
        forms=['condition','procedure'],span='within_document',difficulty='medium',mappings=[['g1']]*9+[['g2']]*5,
        multi='同一生成内容及导出文件的完整标识任务，第4条给显式呈现和文件保留，第5条给元数据隐式信息及水印力度；互不替代，不包括旧题第9条例外申请。')
    change(48, '删除强制交易无关问项，补重大复杂众多消费者的抽检条件，合并第47条并拆途径。',
        question='消费争议需要鉴定、检测商品或服务质量时，机构如何确定？协商不成，以及争议重大复杂且涉及众多消费者权益时，行政部门分别可以怎样处理？',
        points=['消费者与经营者可协商确定鉴定检测机构',
                '无法协商一致时，受理投诉的市场监管或其他有关行政部门可指定机构',
                '重大、复杂、涉及众多消费者权益的争议可由有关行政部门纳入抽查检验程序',
                '抽查检验程序委托具备相应资质的机构鉴定检测'],
        bundles=[b('消费者权益保护法实施条例_',78,79)],forms=['condition','procedure'])
    change(49, '去除车联网元数据半题，范围只问日志存储访问及模块保护；补应保护模块，明确非易失介质、至少6个月和宜定期备份。',
        question='数据库联网日志审计中，仅就日志的访问和完整性保护、存储介质与保存时间、审计模块防护及备份而言，有哪些要求？',
        points=['仅允许授权管理员访问审计日志','日志审计模块应有严格访问控制与完整性保护机制',
                '采用独立审计设备或专用管理网络，确保日志不可被非法删除、篡改或绕过',
                '日志应存于掉电非易失性介质','保存时限不能低于6个月','应对日志审计模块采取安全保护措施',
                '宜使用独立于数据库服务器部署的数据库审计设备','宜定期备份日志'],
        bundles=[b('数据库联网安全要求',116,119,122,123,124)],forms=['condition'],
        scope='明确只考5.6中访问完整性、介质期限和模块防护备份，不声称涵盖查询分析功能、全量联网行为记录或策略评估。')
    change(51, '与本批0022表格属性题区分：仅考7.3完整实施程序和其余指标取舍原则；删除旧运营能力任务。',
        question='工业互联网平台服务商评价实施，从明确目的原则到给出意见建议，宜按怎样的步骤推进？在指标框架中，必选指标之外的其他二级指标可按什么需要取舍？',
        points=['首先明确评价目的和原则','确定评价指标体系框架',
                '其余二级指标可根据行业或地方业务特点及评价需要适当取舍',
                '合理设定一级和二级各项指标权重','设计问卷问题并明确问题分值，向被评价对象发放问卷',
                '收回问卷并完成数据处理与分数计算','按总体得分划分评价等级',
                '基于各项得分和等级开展对标分析并提出意见建议'],
        bundles=[b('平台服务商评价方法',137,138,139,140,141,142,143,144)],forms=['procedure','condition'],
        scope='仅考宜采用的实施程序及其他二级指标取舍依据，不要求重复列附录A的19个名称、采集项或已覆盖运营能力。')
    change(52, '删旧终端体系任务，明确信息通信融合下的信息消费个人家庭应用，并按措施与示例拆点。',
        question='推动人工智能与信息通信融合时，基础电信企业在丰富信息消费场景、创新个人和家庭智能体应用方面，有哪些鼓励措施和列举的应用？',
        points=['鼓励基础电信企业用人工智能赋能传统电信业务','加强基于智能体的新型个人和家庭应用创新',
                '深化智慧个人助理应用','深化智慧管家应用','深化家庭看护应用','深化互动健身应用','深化3D观影等应用',
                '拓展消费服务新场景，提升生活品质'],
        bundles=[b('“人工智能+信息通信”创新发展实施意见.pdf',29)],scope='只问信息消费段，不再问具身智能通信终端布局；应用带等，非穷举。')
    change(54, '删除全球合作独立任务，只保留AI+人才队伍建设跨页完整段，并拆培养、评价激励措施。',
        question='“人工智能+”行动在人才队伍建设方面，对教育培养、人才评价与探索空间、企业中长期激励提出了哪些措施？',
        points=['推进人工智能全学段教育','推进全社会通识教育','完善学科专业布局','加大高层次人才培养力度',
                '超常规构建领军人才培养新模式','强化师资力量建设','推进产教融合','推进跨学科培养','推进国际合作',
                '完善符合AI人才职业属性和岗位特点的多元评价体系','更好发挥领军人才作用',
                '给予青年人才更大施展空间','鼓励积极探索人工智能无人区',
                '支持企业规范用好股权、期权等中长期激励，引才留才用才'],
        bundles=[b('国务院关于深入实施',42,43)])
    change(55, '删除拟人化退出拼接，芯片5.1 b)引用实际禁止功能；不把该指标误称成一条笼统无接口要求，测评保持定位和流量实文。',
        question='人工智能加速芯片对远程关闭、定位及未声明实体通信有什么硬件安全要求？检查定位装置和互联网通信流量时应怎样测评，预期结果是什么？',
        points=['不应提供远程关闭功能','不应提供定位功能','不应提供与未声明通信实体交互数据等功能',
                '通过专业设备扫描芯片是否插装定位装置','芯片联通互联网后检测可疑进出流量',
                '预期芯片没有插装定位装置','预期不存在与未声明实体通信的可疑流量'],
        bundles=[b('加速芯片安全功能技术规范',76,78),b('加速芯片安全功能技术规范',115,118,119,120)],
        forms=['procedure','fact'],span='within_document',difficulty='medium',mappings=[['g1']]*3+[['g2']]*4,
        multi='同一硬件安全测评需5.1 b)的禁止功能实文和6.1的对应定位流量测试方法及预期结果；不推断未提供的远程关闭测试步骤。')
    change(56, '删除重复题面目的作为必要点，只考利益分享机制名称。',
        points=['可以构建利益分享机制'],bundles=[b('科学技术进步法_',176)])
    change(59, '删除旧题完整应用环节及30—41代码半题，聚焦未被旧题核心答案覆盖的27—29实际归类；补表头。',
        question='平台对三项行业型解决方案填写基础标签的服务行业编码，分别服务有色金属冶炼和压延加工业、金属制品业、通用设备制造业。各应填什么编码？',
        points=['有色金属冶炼和压延加工业对应27','金属制品业对应28','通用设备制造业对应29'],
        bundles=[b('平台解决方案分类方法',144,162,163,164)],forms=['condition'],
        scope='只做指定三个行业的业务标签归类，不查询标准编号，不重考旧题30—41、99或完整应用环节分类。')
    change(61, '生态全清单收窄为运营分析与动态监测一项，补监测平台、数据共享、榜单地图及流通管理完整内容。',
        question='工业互联网平台建设推广中，开展平台运营分析与动态监测，要搭建什么平台、怎样共享监测数据、发布或开发哪些榜单地图，并怎样推进工业大数据管理与流通？',
        points=['搭建监测分析服务平台','加强工业互联网平台运营数据共享','实时动态监测平台发展情况',
                '发布工业APP订阅榜','发布平台用户地图等榜单','开发细分行业产能分布数字地图',
                '加强工业大数据管理与新技术应用','推进平台间数据安全流动','推进可信交易','推进汇聚共享','推进服务增值'],
        bundles=[b('工业互联网平台建设及推广指南',28)],scope='只考运营分析动态监测，不再声称覆盖平台全部生态、安全管理或互联互通要求。')
    change(62, '删除独立纠纷路径问项；保留同一音乐制作到录音制品复制发行权利链，补前款制作者许可并拆许可付酬和禁用例外。',
        question='录音制作者使用他人已合法录制为录音制品的音乐作品制作新录音制品，在著作权人许可、付酬及禁用声明方面有什么规则？随后复制、发行录音录像制品时，需要哪些权利人的许可及付酬？',
        points=['使用他人已合法录制的音乐作品制作录音制品，可以不经著作权人许可',
                '但应按规定支付报酬','著作权人声明不许使用的，不得使用',
                '复制发行录音录像制品须取得录音录像制作者许可','还须同时取得著作权人许可',
                '还须同时取得表演者许可','应支付报酬'],
        bundles=[b('著作权法_',168,169),b('著作权法_',171,172)],forms=['condition','procedure'],
        span='within_document',difficulty='medium',mappings=[['g1']]*3+[['g2']]*4,
        multi='同一音乐使用链的两个环节：第42条合法录音音乐再制作例外，第44条既有录音录像制品复制发行许可，二者对象和许可主体不互相替代。',
        scope='不把音乐作品再制作的著作权许可例外扩成使用既有录音全部免许可；只考所问权利，不作为含第43条表演者合同在内的一切制作义务清单。')
    change(63, '删除重复适用对象及所有防护泛问，完整第六节远程访问：高风险服务禁止、确需加固和日志审计。',
        question='工业控制系统远程访问安全对面向互联网开通HTTP、FTP、Telnet等服务有什么原则？确需远程访问或远程维护时，应如何加固、限制和接入，并保留审计什么？',
        points=['原则上严格禁止面向互联网开通HTTP、FTP、Telnet等高风险通用网络服务',
                '确需远程访问时采用数据单向访问控制等策略安全加固','控制访问时限','采用加标锁定策略',
                '确需远程维护时采用VPN等远程接入方式','保留相关访问日志','对操作过程安全审计'],
        bundles=[b('工业控制系统信息安全防护指南',34,35,36,37,38)],forms=['condition','procedure'])
    return items, notes


def scan_boundaries(corpus, items):
    patterns = {
        '0010':[r'大二层',r'多园区统一组网|海量物联设备'],
        '0025':[r'生成式人工智能.{0,80}分类分级|分类分级.{0,80}生成式人工智能',r'分类分级监管|监管规则|监管.*(?:等级|级别)'],
        '0035':[r'跨境|出境|境外提供',r'审批|申报|受理|办理',r'(?:评估|审批|办理|备案).{0,60}(?:天|日)|(?:天|日).{0,60}(?:评估|审批|办理|备案)'],
        '0040':[r'安全监测和应急预案演练',r'深度包检测|应急响应预案|立即采取紧急防护'],
    }
    records = {}
    for item in items:
        if item['expected_behavior']=='answer':
            continue
        selected = patterns[item['id'].rsplit('-',1)[1]]
        hits=[]
        for doc in corpus.documents.values():
            for pos, block in enumerate(doc['blocks']):
                text=re.sub(r'\s+','',block['text'])
                matched=[p for p in selected if re.search(p,text)]
                if matched:
                    hits.append(dict(doc_id=doc['doc_id'],source_path=doc['source_path'],
                        document_sha256=doc['sha256'],block_id=block['block_id'],text=block['text'],patterns=matched,
                        context=doc['blocks'][max(0,pos-1):pos+2]))
        records[item['id']]=dict(patterns=selected,hits=hits,reviewed_conclusion=item['boundary_reason'],
            coverage='All parsed blocks in 33 fixed documents; keyword search and direct reading of relevant provisions; not a web/current-law survey.')
        # Core exporter expects corpus_search to use chunk coordinates.
        affected={(h['doc_id'],h['block_id']) for h in hits}
        item['corpus_search']=[dict(chunk_id=c['chunk_id'],score=1.0,method='literal_fixed_corpus_scan')
            for c in corpus.chunks.values() if any((c['doc_id'],s['block_id']) in affected for s in c['sources'])]
        assert item['corpus_search']
    return dict(corpus=corpus.identity(), results=records)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='data/benchmark/batch_002_review64/revision_v2')
    parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    output=(ROOT/args.output).resolve()
    assert output.is_relative_to(ROOT) and output!=SOURCE
    assert args.check_only or not output.exists(), f'Refusing to overwrite {output}'
    corpus=Corpus(ROOT/'data/processed/baseline_v1')
    original=read_jsonl(SOURCE/'candidates.jsonl')
    audit=read_jsonl(SOURCE/'agent_review_v1/audit.jsonl')
    wanted={r['ordinal'] for r in audit if r['agent_review_status']=='needs_revision'}
    assert len(wanted)==42
    all_items, notes=revise(corpus,original)
    assert set(notes)==wanted, (set(notes)-wanted,wanted-set(notes))
    candidates=[i for n,i in enumerate(all_items,1) if n in wanted]
    search=scan_boundaries(corpus,candidates)
    gold=read_jsonl(ROOT/'data/benchmark/batch_002_64/input/gold86.jsonl')
    keep12=[copy.deepcopy(original[r['ordinal']-1]) for r in audit if r['agent_review_status']=='approved']
    _, links=duplicate_clusters(gold+keep12+candidates)
    revised_ids={i['id'] for i in candidates}
    active_links=[p for p in links if any(identifier in revised_ids for identifier in p)]
    # Keep semantic false positives reviewable rather than silently changing thresholds.
    for i in candidates:
        validate_item(i,corpus)
    if args.check_only:
        print(json.dumps(dict(revised=len(candidates),automatic_duplicate_links=active_links,
            labels=dict(Counter(i['evidence_span'] for i in candidates))),ensure_ascii=False,indent=2))
        return
    assert not active_links, active_links
    protected={str(p.relative_to(ROOT)):file_hash(p) for p in SOURCE.rglob('*') if p.is_file()}
    old_protection=json.loads((ROOT/'data/benchmark/batch_002_64/input/protected_sha256.json').read_text(encoding='utf-8'))
    frozen=old_protection.get('files',old_protection)
    for name,value in frozen.items():
        expected=value.get('sha256') if isinstance(value,dict) else value
        assert file_hash(ROOT/name)==expected,name
        protected[name]=expected
    stamp=now()
    logs=[]
    fields=['user_input','reference','answer_points','evidence_groups','category','answer_form','evidence_span',
            'difficulty','query_style','expected_behavior','scope_note','point_evidence','multi_source_reason','boundary_reason','clarifying_interpretations']
    for n in sorted(wanted):
        item=all_items[n-1]
        old=original[n-1]
        assert content_fingerprint(item)!=content_fingerprint(old),item['id']
        historical={k:copy.deepcopy(old[k]) for k in ('semantic_precheck','boundary_precheck','machine_notes') if k in old}
        for k in ('semantic_precheck','boundary_precheck','machine_notes'):
            item.pop(k,None)
        item['generation_context_provenance']=dict(original_node_ids=old.get('node_ids',[]),
            original_chunk_ids=old.get('chunk_ids',[]),
            original_generator_metadata={k:copy.deepcopy(old[k]) for k in ('pilot','relation','synthesizer','generation_targets','search_queries') if k in old},
            status='Historical only; revised evidence_groups and current boundary scan are authoritative support.')
        for k in ('node_ids','chunk_ids','scenario','reference_contexts','contexts','pilot','relation','synthesizer','generation_targets','search_queries'):
            item.pop(k,None)
        item.update(review_status='pending',reviewer='',review_notes='',review_checks={},duplicate_group='',
            revision_note=notes[n],source_id=old['id'],source_candidate_sha256=digest(old),
            revision_provenance=dict(method='source_grounded_revision',author='Codex',authorization=AUTH,
                human_per_item_review_claimed=False,source_batch=str(SOURCE.relative_to(ROOT)),
                source_agent_review_sha256=digest(audit[n-1]),created_at=stamp),evidence_audit=evidence_audit(item,corpus))
        if item['expected_behavior']!='answer':
            item['corpus_search_provenance']=dict(method='literal_fixed_corpus_scan',
                current_search_file='boundary_search_audit.json',score_semantics='1.0 means literal-match presence, not retrieval relevance',
                reviewed_at=stamp)
        validate_item(item,corpus)
        changed=[k for k in fields if old.get(k)!=item.get(k)]
        logs.append(dict(ordinal=n,id=item['id'],source_candidate_sha256=digest(old),
            source_content_sha256=content_fingerprint(old),revised_content_sha256=content_fingerprint(item),
            changed_fields=changed,changes={k:dict(before=old.get(k),after=item.get(k)) for k in changed},
            revision_note=notes[n],source_findings=audit[n-1],historical_machine_precheck=historical,
            source_status='needs_revision',authorization=AUTH,
            self_check='题面范围、逐项支持、漏项、义务力度、连续证据单元、旧86及保留12/修订42同批核心任务复核。非独立人工复审。'))
    output.mkdir(parents=True)
    write_jsonl(output/'candidates.jsonl',candidates)
    write_json(output/'boundary_search_audit.json',search)
    export_review(candidates,output/'review',corpus)
    csv_path=output/'review/review.csv'
    with csv_path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f); columns=reader.fieldnames; csv_rows=list(reader)
    for row,item in zip(csv_rows,candidates):
        checks={k:True for k in (*REVIEW_CHECKS,*CONTENT_CHECKS)}
        checks['corpus_search']=item['expected_behavior']!='answer'
        row.update(review_status='approved',reviewer=REVIEWER,
            review_checks=json.dumps(checks,ensure_ascii=False),
            review_notes=item['revision_note']+'\n用户明确授权修订及同一agent自检；approved仅为本轮自检通过，不声称用户逐题审阅或独立人工复审，不自动冻结。')
    with csv_path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,columns);writer.writeheader();writer.writerows(csv_rows)
    reviewed=import_review(candidates,csv_path,corpus)
    for item,log in zip(reviewed,logs):
        item['review_provenance']=dict(method='user_authorized_revision_self_check',reviewer=REVIEWER,
            authorization=AUTH,human_per_item_review_claimed=False,independent_reviewer=False,
            scope='Only the 42 needs_revision items; no gold freeze or dev/holdout split',audit_file='revisions.jsonl')
        item['approval_provenance']={**item['review_provenance'],'at':item['reviewed_at']}
        validate_item(item,corpus,approved=True)
        log.update(final_review_status=item['review_status'],revised_candidate_sha256=digest(item),
            review_checks=item['review_checks'],evidence_audit=evidence_audit(item,corpus))
    assert len(reviewed)==42 and all(i['review_status']=='approved' for i in reviewed)
    write_jsonl(output/'reviewed.jsonl',reviewed)
    write_jsonl(output/'approved.jsonl',reviewed)
    write_jsonl(output/'revisions.jsonl',logs)
    export_review(reviewed,output/'review_after_import',corpus)
    # Full-batch view replaces only the requested 42 objects. The other 22 remain byte-equivalent as JSON objects.
    revised_by_id={i['id']:i for i in reviewed}
    full=[revised_by_id.get(i['id'],copy.deepcopy(i)) for i in original]
    assert all(full[n-1]==original[n-1] for n in range(1,65) if n not in wanted)
    for item in full:
        validate_item(item,corpus,approved=item['review_status']=='approved')
    write_jsonl(output/'full_batch64.jsonl',full)
    remaining=dict(original_pass_suggestions=[r['id'] for r in audit if r['agent_review_status']=='approved'],
        original_exclusion_suggestions=[r['id'] for r in audit if r['agent_review_status']=='rejected'],
        original_statuses_unchanged=True,
        note='Only 42 revisions authorized and processed here. The remaining 12 suggested pass items and 10 suggested exclusions retain original pending state; do not select the 10 excluded IDs for the final pool.')
    write_json(output/'remaining22.json',remaining)
    overlaps=[]
    def coords(i):
        return {(e['doc_id'],e['block_id']) for g in i['evidence_groups'] for bundle in g['alternatives'] for e in bundle}
    for item in reviewed:
        for anchor in gold+keep12:
            shared=coords(item)&coords(anchor)
            if shared:
                overlaps.append(dict(candidate_id=item['id'],reference_id=anchor['id'],same_core=False,
                    shared_blocks=[dict(doc_id=d,block_id=bid) for d,bid in sorted(shared)],
                    decision='按指定题面与最小必要答案核验为不同任务或保留新增子集；共享表格、背景条款不自动判同核心。'))
    write_json(output/'duplicate_relations.json',dict(reference_gold_count=86,original_keep12_count=12,
        revised_count=42,automatic_remaining_links=active_links,partial_evidence_overlaps=overlaps,
        within_batch_resolution=[
            dict(ids=['batch_002_64-0019','batch_002_64-0047'],decision='0019只问防歧视和质量透明，0047只问具体标识，不再共享核心标识义务'),
            dict(ids=['batch_002_64-0022','batch_002_64-0052'],decision='0022考附录完整19指标选择属性，0052考实施流程与其他指标取舍，不重复评分名称清单'),
            dict(ids=['batch_002_64-0024','batch_002_64-0046'],decision='0024考资源管理能力，0046考除存储计算外的应用服务能力，按不同能力枚举单元区分')],
        old_overlap_resolution=[
            dict(id='batch_002_64-0004',decision='删旧trial0014芯片加密，仅保留平台加密存储'),
            dict(id='batch_002_64-0018',decision='删旧trial0016加密协议密钥，仅保留跨安全域及大量个人信息跨境'),
            dict(id='batch_002_64-0033',decision='删旧trial0029个人信息，保留违法处置与投诉机制'),
            dict(id='batch_002_64-0046',decision='删旧trial0006跨行业及batch0010022存储计算'),
            dict(id='batch_002_64-0053',decision='删旧trial0021终端产品，保留个人家庭信息消费'),
            dict(id='batch_002_64-0060',decision='仅27—29归类，删旧batch0010034完整场景类别及0002的30—41、99'),
            dict(id='batch_002_64-0064',decision='删旧trial0020适用对象，保留第六节远程访问安全')]))
    corrections=[dict(id='batch_002_64-0015',prior='上一轮称拟人化互动服务来源为草案',
        correction='实际引用为第21号令、2026年第17号国务院公报的人工智能拟人化互动服务管理暂行办法，正文未标草案。只纠正这一题的来源身份，不改历史审核记录。'),
        dict(id='batch_002_64-0056',prior='上一轮将5.1 b)概括为无远程控制接口',
        correction='原第6.1测评小节确有无远程控制接口字样，但5.1 b)实质禁止远程关闭、定位、未声明实体交互；修订答案给三类实文及定位/流量实际测试，不扩大或混淆二者。'),
        dict(id='batch_002_64-0057',prior='上一轮说明误称题面目的为科技成果产业化受益',
        correction='实际题面目的为人才、技术、资金等要素自由流动；删除重复目的评分的结论仍成立，修订只考利益分享机制。')]
    write_json(output/'review_corrections.json',corrections)
    pdf=json.loads((SOURCE/'agent_review_v1/pdf_checks.json').read_text(encoding='utf-8'))
    pages={('工业互联网平台监测分析指南.pdf',9),('工业互联网平台服务商评价方法.pdf',13),
        ('工业互联网平台服务商评价方法.pdf',14),('工业互联网园区网络白皮书.pdf',49),
        ('工业互联网平台解决方案分类方法.pdf',11),('工业互联网平台解决方案分类方法.pdf',12)}
    pdf=[p for p in pdf if (Path(p['source_path']).name,p['physical_page']) in pages]
    assert len(pdf)==6
    for entry in pdf:
        assert file_hash(ROOT/entry['source_path'])==entry['source_sha256']
        entry['revision_visual_check']=True
    write_json(output/'pdf_checks.json',pdf)
    source_manifest=json.loads((SOURCE/'manifest.json').read_text(encoding='utf-8'))
    shutil.copyfile(SOURCE/'families.json',output/'families.json')
    for name,expected in protected.items():
        assert file_hash(ROOT/name)==expected,name
    summary=dict(status='42_source_grounded_revisions_and_authorized_self_check_complete',
        total=42,review_status=dict(Counter(i['review_status'] for i in reviewed)),
        revised_content_count=len(logs),remaining_needs_revision=0,
        labels=dict(category=dict(Counter(i['category'] for i in reviewed)),
            evidence_span=dict(Counter(i['evidence_span'] for i in reviewed)),
            expected_behavior=dict(Counter(i['expected_behavior'] for i in reviewed)),
            difficulty=dict(Counter(i['difficulty'] for i in reviewed))),
        validation=dict(items=42,csv_import_round_trip=True,original22_items_unchanged=True,
            no_automatic_duplicates_against_gold86_and_keep12=True,semantic_core_review=True,
            boundary_items_searched=len(search['results']),fixed_documents=len(corpus.documents),visual_pdf_pages=len(pdf),
            protected_input_files_unchanged=len(protected)),
        authorization=AUTH,reviewer=REVIEWER,human_per_item_review_claimed=False,independent_reviewer=False,
        frozen_gold_count=86,target_gold_count=150,no_freeze_performed=True,external_llm_calls=0)
    write_json(output/'summary.json',summary)
    write_json(output/'manifest.json',dict(schema_version=2,status=summary['status'],created_at=stamp,
        source_batch=str(SOURCE.relative_to(ROOT)),source_candidates_sha256=file_hash(SOURCE/'candidates.jsonl'),
        source_agent_audit_sha256=file_hash(SOURCE/'agent_review_v1/audit.jsonl'),corpus=corpus.identity(),
        authorization=AUTH,script_sha256=file_hash(__file__),protected_artifact_hashes=protected,
        no_freeze_performed=True,source_generation_manifest_sha256=digest(source_manifest)))
    lines=['# 新增42题修订 v2','',
        '**42道需修订题均已按原文修订并完成用户授权自检，本轮42道approved；未冻结为金标准。**','',
        '授权原文：'+AUTH,'',
        'approved仅表示用户授权Codex修订与同一agent自检通过；不是用户逐题审阅或独立人工复审。既有86题金标准、原始64候选和agent_review_v1均未修改。其他12道建议通过和10道建议排除不在本次改写范围。','',
        '## 阅读入口','',
        '- `review_after_import/reading.md`：42题最终题面、答案、评分点、逐字证据和自检意见。',
        '- `review/review.csv`：对应本版candidates.jsonl的已填审核表，可在之后的新输出中继续复核、修改。',
        '- `approved.jsonl` / `reviewed.jsonl`：42题授权自检结果。',
        '- `revisions.jsonl`：42题每个内容字段的前后差异、原审核问题与来源哈希。',
        '- `full_batch64.jsonl`：按原序号合入42题的整批视图，其他22题对象与原始版本完全一致。',
        '- `remaining22.json`：12题建议通过、10题建议排除的ID；整批视图中这22题保持pending，不表示10题可重新选入。',
        '- `boundary_search_audit.json`：4题边界题的33份固定文档检索、上下文及判定。',
        '- `duplicate_relations.json`：与旧86题、保留12题、修订题内部的去重及局部重叠说明。',
        '- `review_corrections.json`：纠正旧审核对拟人化办法来源、芯片接口概括、利益分享题目的不准确措辞。','',
        '## 本轮处理','',
        '过宽范围收窄到可完整回答的实际任务；去掉无关跨领域并列问项和已被旧题覆盖的半题。同一法条、连续小节清单或表格作为single_unit，不为凑配额保留伪多跳。保留同一测评、违规处置、权利链或保护方案中不可替代的独立单元。','',
        '0008补设备连接全口径及两个二级维度；0022补完整19个指标的必选可选属性，不再询问采集项。0063补录音录像制作者许可。0015和0030分别由虚假澄清改为禁止提供及指标事实；0025由无反证纠错改为拒答，0035拒答只限具体流程时限。','',
        '0034收窄为信息通信2028/2030目标，分类改policy。工业企业分类分级指南的征求意见稿身份保留；拟人化办法实际第21号令来源身份已纠正。时限、阈值、当前执行状态不从其他领域或现实猜测补入。','',
        '## 逐题修订','', '|原序号|ID|本轮状态|修订|','|---|---|---|---|']
    for log in logs:
        lines.append(f"|{log['ordinal']}|{log['id']}|approved|{log['revision_note'].replace('|','/')}|")
    lines+=['','## 校验与数量','',
        '全部42题通过schema、原文字符区间、文档哈希、逐点评分映射、标签与审批字段校验；审核CSV实际导入且再次导出。与旧86及本批保留12题自动去重无链，并按核心答案核对局部重叠。4道边界题重做固定全库检索。4份PDF的6页表头、合并单元格和续表已视觉核对。','',
        '当前冻结金标准仍86题。若其他12题复审接纳，再接纳本轮42题，并替换被排除的10题，才能达到150；本次不执行额外生成、冻结或dev/holdout划分。','',
        '## 重现','', '在项目根目录的conda rag环境运行，必须使用不存在的新输出目录：','',
        '```powershell','python scripts/revise_benchmark_batch002.py --output data/benchmark/batch_002_review64/revision_v2_replay','```','',
        '可先使用--check-only执行内存构建及结构/去重检查；不写输出、不调用模型。']
    (output/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    shutil.copyfile(__file__,output/'assemble_revision.py')
    write_json(output/'artifact_hashes.json',{str(p.relative_to(output)):file_hash(p) for p in sorted(output.rglob('*')) if p.is_file()})
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
