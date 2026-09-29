"""Source-grounded batch_001 revision; never overwrite historical artifacts.

Run from repository root with conda rag. No LLM/API calls or corpus mutations.
"""
import argparse
import copy
import csv
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag_baseline.benchmark.core import (Corpus, REVIEW_CHECKS, digest, duplicate_clusters,
    export_review, file_hash, import_review, now, read_jsonl, validate_item, write_json, write_jsonl)
from rag_baseline.benchmark.quality import CONTENT_CHECKS, content_fingerprint, evidence_audit

AUTH = '参考Claude Code复核结果，按你自己的判断修改有问题的题目，已由我人工授权'
SOURCE = Path('data/benchmark/batch_001_review60')
REVIEWER = 'Codex（用户授权修订及自检；非独立人工复审）'


def revise(corpus, originals):
    items = copy.deepcopy(originals)
    notes = {}

    def doc(term):
        ds = [d for d in corpus.documents.values() if term in Path(d['source_path']).name]
        if len(ds) != 1:
            raise ValueError(f'Ambiguous document {term}: {[d["source_path"] for d in ds]}')
        return ds[0]['doc_id']

    def b(term, *ids):
        did = doc(term)
        return [corpus.evidence(did, bid, corpus.blocks[did, bid]['text']) for bid in ids]

    def original_bundle(n, *groups):
        return [copy.deepcopy(e) for g in items[n-1]['evidence_groups'] if g['id'] in groups
                for e in g['alternatives'][0]]

    def change(n, note, question=None, points=None, bundles=None, forms=None,
               span='single_unit', difficulty='easy', scope=None, multi='', mappings=None,
               behavior='answer', reference=None):
        i = items[n-1]
        if question is not None:
            i['user_input'] = question
        if points is not None:
            i['answer_points'] = points
        i['reference'] = reference if reference is not None else '；'.join(i['answer_points']) + '。'
        if bundles is not None:
            i['evidence_groups'] = [{'id': f'g{k+1}', 'alternatives': [bundle]} for k,bundle in enumerate(bundles)]
        i.update(answer_form=forms or ['fact'], evidence_span=span, expected_behavior=behavior,
                 difficulty=difficulty if behavior == 'answer' else None,
                 difficulty_reason=('从一个完整条款、枚举或表格单元中提取事实或适用给定条件。'
                     if span == 'single_unit' else '需结合不同必要证据单元，完成同一任务。'),
                 scope_note=scope or i['user_input'], multi_source_reason=multi,
                 boundary_reason='', clarifying_interpretations=[], corpus_search=[])
        i['point_evidence'] = mappings or [['g1'] for _ in i['answer_points']]
        if mappings is None and len(i['evidence_groups']) > 1:
            raise ValueError(f'Explicit mapping required: {n}')
        notes[n] = note

    # Keep all original IDs; version and fingerprints disambiguate revisions.
    change(2, '补表头与行业型分类锚点；映射不变。合并单元格覆盖11—99，不仅30—41。',
           bundles=[b('解决方案分类方法',144,145,*range(165,178))])
    change(3, '删除与约谈不构成共同任务的实名处罚问项，保留安全风险监督措施。',
           question='省级以上人民政府有关部门在履行网络安全监督管理职责时，发现网络存在较大安全风险或发生安全事件，可以约谈谁？网络运营者随后应当怎样处理？',
           points=['可以按照规定的权限和程序约谈该网络运营者的法定代表人或者主要负责人',
                   '网络运营者应当按要求采取措施、整改并消除隐患'],
           bundles=[b('网络安全法_',112)],forms=['condition','procedure'])
    change(4, '纠正初审multi_source误报；重写已有有线网络的园区无线建设任务，补271无线建设内容，删除无证据的必然关联。',
           question='一个已有有线网络的工业园区拟新增无线覆盖。采用AC/AP叠加式架构时，AP与AC如何连接，小型二层网络和大型三层网络各有什么回传条件？在选择园区无线覆盖技术时，又应考虑哪些建设目标、技术组合和约束？',
           points=['AC/AP叠加式架构用CAPWAP隧道连接AP和AC，可叠加在已有有线网络上',
                   '小型二层网络中AC和FIT AP处于同一广播域，AP可通过本地广播找到AC',
                   '小型网络的AC与FIT AP之间可部署专用回传网络',
                   '大型网络通常借用有线网络回传',
                   '三层组网要求AP与AC路由可达，通过三层CAPWAP建立回传通道',
                   '园区网络建设要求高可靠、大带宽并实现无死角覆盖',
                   '可利用运营商5G、NB-IoT网络并部署WiFi6网络实现覆盖',
                   '企业可按业务需求和预算选择5G与WiFi6综合构建可靠、大带宽、高性价比的无线网络'],
           bundles=[b('园区网络白皮书',364,365,366,367),b('体系架构（版本2.0）',270,271)],
           forms=['condition','synthesis'],span='cross_document',difficulty='medium',
           multi='同一已有有线园区的无线建设决策：白皮书给AC/AP回传条件，体系架构给覆盖技术与需求预算约束；两份引用各自不能完整回答。CAPWAP与WiFi6并非冗余内容，但不推断所有覆盖技术都使用AC/AP。',
           mappings=[['g1']]*5+[['g2']]*3,
           scope='只考AC/AP回传与园区无线覆盖选择，不问全部园区设备、SDN管理或云边协同；不宣称5G/NB-IoT采用AC/AP架构。')
    change(5, '重复排除不变；同一5.5条a/b合并为一组，统一single_unit口径。',
           bundles=[b('数据库联网安全要求',109,110,111)])
    change(6, '将全套上线条件收窄为个人信息总则的处理原则；第六条连续两段合为一个单元，拆开原子要点。',
           question='个人信息处理的总则对合法诚信、目的和最小必要、公开透明、信息质量、安全责任及禁止行为，分别规定了什么？',
           points=['遵循合法、正当、必要和诚信原则','不得以误导、欺诈、胁迫等方式处理个人信息',
                   '处理目的应明确合理，并与处理活动直接相关','采取对个人权益影响最小的方式',
                   '收集限于实现目的的最小范围，不得过度收集','公开透明，公开处理规则并明示目的、方式和范围',
                   '保证信息质量，避免不准确、不完整对个人权益产生不利影响',
                   '处理者对处理活动负责并采取必要措施保障信息安全',
                   '不得非法收集、使用、加工、传输或非法买卖、提供、公开他人个人信息',
                   '不得从事危害国家安全、公共利益的个人信息处理活动'],
           bundles=[b('个人信息保护法_',25),b('个人信息保护法_',26,27),b('个人信息保护法_',28),
                    b('个人信息保护法_',29),b('个人信息保护法_',30),b('个人信息保护法_',31)],
           forms=['fact'],span='within_document',difficulty='medium',
           mappings=[['g1'],['g1'],['g2'],['g2'],['g2'],['g3'],['g4'],['g5'],['g6'],['g6']],
           multi='共同梳理总则第5—10条的处理原则，各条分别规定不同方面；第6条不再按段落拆组。',
           scope='题面限定总则处理原则，不作为用户画像工具完整上线清单；合法基础、告知、自动决策和敏感信息另有条款。')
    change(7, '7.1.1同一完整枚举合为一组；移除无关附件示例，保持静态打标要求。',
           bundles=[b('网络数据标签标识技术要求',182,183,184,187,188)],forms=['fact','condition'])
    change(8, '取消解读与法规正文冗余cross_document；保留监管方式和用户救济同一监督场景，删除与旧0017重叠的内容合规清单。',
           question='生成式人工智能服务的监督管理由哪些部门按职责承担，如何适应技术和行业应用特点？使用者发现服务违规，可以向谁投诉或举报？',
           points=['网信、发展改革、教育、科技、工业和信息化、公安、广播电视、新闻出版等部门按各自职责依法管理',
                   '有关主管部门完善适应技术特点及行业领域应用的科学监管方式',
                   '制定相应的分类分级监管规则或指引',
                   '使用者发现服务不符合法律、行政法规或暂行办法时，可向有关主管部门投诉、举报'],
           bundles=[original_bundle(8,'g2'),original_bundle(8,'g4')],span='within_document',difficulty='medium',
           mappings=[['g1']]*3+[['g2']],multi='监管职责和分类监管规定与用户投诉举报权共同构成违规服务监督场景，分属第16、18条。')
    change(9, '取消无反证的correct_premise；改考平台互联互通的政策建设措施，不查询未提供的准则名称或现实实施状态。',
           question='为推动工业互联网平台之间的数据与服务互联互通，建设推广指南提出了哪些规范、数据迁移和软件跨平台调用措施？',
           points=['制定平台互联互通规范，构建公平、有序、开放的发展环境',
                   '制定发布平台数据迁移行业准则，以实现不同平台间工业数据自由传输迁移',
                   '支持协会联盟制定软件跨平台调用标准',
                   '推动工业模型、微服务组件和工业APP跨平台可部署、可调用、可订阅'],
           bundles=[original_bundle(9,'g1')],scope='固定建设推广指南提出的措施，不推断行业准则是否已发布或当前应遵循的具体版本。')
    change(10, '补统计表头，明确一级指标列；不改正确的服务企业统计口径。',
           bundles=[b('平台监测分析指南',134,135,136)],forms=['fact'])
    change(11, '将全部主体职责收窄到质量投诉处理共同任务；补第32条及协会主体锚定。',
           question='消费者反映商品质量问题时，有关行政部门应如何调查处理及处置危及人身财产安全的缺陷？消费者协会在受理该投诉、调查调解和质量鉴定方面有什么职责？',
           points=['有关行政部门听取消费者、消费者协会等组织的意见并及时调查处理',
                   '行政部门发现并认定商品或服务存在危及人身财产安全的缺陷时，立即责令经营者采取停止销售、警示、召回、无害化处理、销毁、停止生产或服务等措施',
                   '消费者协会受理消费者投诉，对投诉事项进行调查、调解',
                   '涉及商品和服务质量问题时，消费者协会可以委托具备资格的鉴定人鉴定',
                   '鉴定人应告知鉴定意见'],
           bundles=[b('消费者权益保护法_2013',78,79),b('消费者权益保护法_2013',80,81),b('消费者权益保护法_2013',86,91,92)],
           forms=['procedure','comparison'],span='within_document',difficulty='medium',mappings=[['g1'],['g2'],['g3'],['g3'],['g3']],
           multi='围绕同一商品质量投诉，比较行政调查/缺陷处置与协会调解/鉴定的职责；不是两份无关职责清单。')
    change(12, '删除标准项目明细表冒充组织实施及跨领域拼接；只考分类分级草案基本原则，合并连续枚举。',
           question='工业互联网企业网络安全分类分级的参考指南提出了哪三项基本原则？行业影响、自评和主管部门的作用分别是什么？',
           points=['企业分级与行业网络安全影响程度相关联，以GB/T 4754-2017为基准细化行业类别，行业影响程度是分级关键参考因素',
                   '行业指导与地方监管相结合，由工业和信息化部指导主管行业领域、地方主管部门指导监管本行政区域',
                   '企业自评与属地核查相结合，企业按细则自评，地方主管部门核查确认'],
           bundles=[b('网络安全分类分级管理指南',14,15,16,17)],
           scope='固定库中标明征求意见稿的指南所述基本原则；不据此宣称个案最终行政等级或最新规则。')
    change(13, '取消错误的自然人/法人决定培训视听作品期限的分支；明确视听作品及所问权利，改为可直接回答的期限比较。',
           question='一部已确定属于视听作品的内部培训视频，其作者署名权、修改权、保护作品完整权，与发表权及复制、发行等财产性权利的保护期有什么区别？若创作完成后50年内一直未发表，后两类权利怎样处理？',
           points=['作者的署名权、修改权、保护作品完整权的保护期不受限制',
                   '视听作品发表权保护期为50年，截止于创作完成后第50年的12月31日',
                   '第十条第一款第5—17项权利保护期为50年，截止于首次发表后第50年的12月31日',
                   '作品自创作完成后50年内未发表的，本法不再保护这些有期限的权利'],
           bundles=[b('著作权法_',97),b('著作权法_',100)],forms=['comparison','condition'],span='within_document',difficulty='medium',
           mappings=[['g1'],['g2'],['g2'],['g2']],multi='第22条规定不受限制的人身权，第23条视听作品段规定有期限权利；共同回答同一视频的期限比较。',
           scope='固定著作权法中的已确定视听作品保护期，不判断委托合同归属或实际届满年份。')
    change(15, '题面收窄到总体价值原则，避免被理解为换脸语音上线全套合规；condition改fact。',
           question='深度合成服务在遵法、社会伦理、政治和舆论价值导向方面，应遵循哪些总体原则？',
           bundles=[original_bundle(15,'g1')],forms=['fact'],scope='只问第4条总体原则，不将其作为人脸、人声编辑服务的完整告知同意、标识、安全评估清单。')
    change(16, '删除章节导航必答分，按每项操作与预期结果分别评分，保留同一固件验证任务的两个单元。',
           points=['安全启动：审查技术方案，了解启动校验链组件并查看组件校验日志',
                   '安全启动：尝试篡改固件、系统引导程序等，观测能否正常启动',
                   '安全启动：查验固件、系统引导程序等是否有数字签名或有效来源真实性验证机制',
                   '安全启动预期：能够看到固件、系统引导程序等完整性校验日志',
                   '安全启动预期：篡改固件或系统引导程序后无法正常启动',
                   '安全启动预期：固件、引导程序均有签名或有效机制可验证来源真实性',
                   '度量验证：审查技术文件，明确固件度量验证过程',
                   '度量验证：验证芯片基于硬件可信根生成固件完整性度量值',
                   '度量验证：验证用户能获取度量值并对比基线验证完整性',
                   '度量验证预期：能够获取固件完整性度量值并通过对比基线等方式验证完整性'],
           bundles=[b('加速芯片安全功能技术规范',197,199,200,201,202,203,204,205,206,207),
                    b('加速芯片安全功能技术规范',209,211,212,213,214)],
           forms=['procedure'],span='within_document',difficulty='medium',mappings=[['g1']]*6+[['g2']]*4,
           multi='安全启动验证启动时的拒绝篡改和来源，度量验证取得完整性度量值，两者共同构成固件验证任务且互不替代。',
           scope='题面只问步骤和预期结果；不将5.3节编号、可信根示例品牌作为独立必答分。')
    change(17, '删除产业生态问项，与补04生态题消解重叠；优化环境题进一步收窄为场景制度、知识产权和标准支撑。',
           question='“人工智能+”行动对应用场景建设制度、知识产权和标准支撑，提出了哪些优化措施？',
           points=['健全人工智能应用场景建设指引','健全应用场景开放度评价与激励政策',
                   '完善应用试错容错管理制度','加强知识产权保护、转化与协同应用',
                   '加快重点领域人工智能标准研制','推进跨行业、跨领域、国际化标准联动'],
           bundles=[original_bundle(17,'g1')],forms=['fact'])
    change(18, '撤销以无个案记录为由abstain；把已发生影响改为完整潜在影响假设，按一级条件作答；法条交叉块用精确片段隔开。',
           question='工业数据分级评估已确认：该数据被篡改、破坏、泄露或非法利用的潜在影响较小，不符合二级或三级条件；对平台正常生产运行影响较小，负面影响、经济损失及恢复代价也较小。按工业数据分级的参考规则，应归入哪个级别？判定依据是什么？',
           points=['应归入一级数据','分级依据是被篡改、破坏、泄露或非法利用后的潜在影响，而非仅看某次事故已经发生的影响',
                   '对平台正常生产运行、企业负面影响或经济损失及恢复代价较小，符合一级数据条件'],
           bundles=[b('工业数据分类分级指南',12),
                    [corpus.evidence(doc('工业数据分类分级指南'),18,'第十一条'+corpus.blocks[doc('工业数据分类分级指南'),18]['text'].split('第十一条')[1])]+b('工业数据分类分级指南',19,20)],
           forms=['condition'],span='within_document',difficulty='medium',mappings=[['g2'],['g1'],['g2']],
           multi='第8条给潜在影响判断基础，第11条给一级条件，同一假设分级任务需要二者；第10条尾句不混入一级组。',
           scope='题面已给完整评估及不触发更高级条件的假设，仅适用固定指南参考规则，不声称现实个案已获行政确认。')
    change(20, '分开云环境和数据库两项条件，增强部分计分粒度。',
           points=['具备云计算运行环境','部署主流数据库系统','提供可灵活调度的计算、存储和网络服务',
                   '满足海量工业数据高并发处理需求','积累存储一定规模的工业数据'],forms=['condition'])
    change(21, '改问重要数据处理者的风险管理共同任务，补第29条及30条报告内容，避免冒充全部义务清单。',
           question='重要数据处理者在数据安全风险管理中，应如何明确责任、开展风险监测和事件处置，以及进行定期评估和报告？风险评估报告应包含哪些内容？',
           points=['明确数据安全负责人和管理机构，落实保护责任','加强风险监测',
                   '发现数据安全缺陷、漏洞等风险时立即补救','发生安全事件时立即处置',
                   '按规定及时告知用户并向有关主管部门报告安全事件',
                   '按规定对数据处理活动定期开展风险评估','向有关主管部门报送风险评估报告',
                   '报告包括重要数据种类、数量','报告包括数据处理活动情况','报告包括面临的安全风险及应对措施等'],
           bundles=[b('数据安全法_',53,54),b('数据安全法_',56),b('数据安全法_',57,58)],forms=['procedure','condition'],
           span='within_document',difficulty='medium',mappings=[['g1']]+[['g2']]*4+[['g3']]*5,
           multi='重要数据风险管理的责任、监测处置、周期评估报告分属第27、29、30条，服务于同一风险管理任务。',
           scope='只问题面列明的风险管理环节，不宣称已列全所有数据处理合规义务。')
    change(22, '去掉拟人化服务跨领域问项，区分事前部署和事中处置；原第24条不再属于修订题范围。',
           question='工业控制系统遭受安全威胁而出现异常或故障时，运营方应立即采取哪些防护、报送及现场保护措施？',
           points=['立即采取紧急防护措施，防止事态扩大','逐级报送直至属地省级工业和信息化主管部门',
                   '注意保护现场，以便调查取证'],bundles=[b('工业控制系统信息安全防护指南',42,43)],forms=['procedure','condition'])
    change(23, '保留有效纠错，删除全文制度和模型审核细节的额外必答分；补独立的全库例外搜索记录。',
           points=['该说法不成立：不诱导沉迷不能免除算法安全主体责任',
                   '算法推荐服务提供者仍须落实算法安全主体责任并建立健全相关管理制度和技术措施',
                   '不得设置诱导沉迷的算法模型是另一项要求，不能替代主体责任制度义务'],
           bundles=[original_bundle(23,'g1'),original_bundle(23,'g2')],forms=['condition'],
           span='within_document',behavior='correct_premise',mappings=[['g1','g2'],['g1'],['g2']],
           multi='第7条主体责任义务与第8条禁诱导沉迷义务需要结合，以纠正用后者替代前者的前提。',
           scope='只考不诱导沉迷是否免除主体责任；不要求全文复述制度、人员或定期审核条目。')
    items[22]['boundary_reason'] = '题面以不诱导沉迷作为免除主体责任的充分条件，与第7条无该豁免的义务和第8条独立禁止义务冲突。'

    change(26, '三趋势改为独立评分点；融合问题限定平台化架构趋势所述机制。230已含融合机制，初审以漏231为由不完整的说法撤回。原文笔误保留在证据，答案作归纳。',
           question='传统ISA-95制造体系在数字化转型中呈现哪三方面演进趋势？其中，平台化架构趋势通过什么机制促进工业软件融合，并带来哪些直接效果？',
           points=['基于平台的数据智能成为制造业智能化的核心驱动','平台化架构成为未来数字化系统的共性选择',
                   '基于平台的应用开放创新','统一平台上的数据集成管理和智能分析应用破解信息孤岛',
                   '在平台上部署研发设计、仿真优化、生产管理和运营管理等软件工具，降低系统复杂程度和投资成本',
                   '打通集成全生产流程的一体化服务能力，促进业务协同'],bundles=[original_bundle(26,'g1')],
           scope='只考三趋势及其中平台化架构段的融合机制；不要求后段ERP/MES/PLM集成，也不要求复述其他趋势全文。')
    change(27, '将赔偿责任难定改为损失及获益难定；同一第69条两段合组single_unit。',
           question='处理个人信息侵害个人信息权益并造成损害时，处理者在什么条件下承担侵权责任？赔偿通常以什么确定，个人损失和处理者获益均难以确定时如何定金额？',
           bundles=[b('个人信息保护法_',184,185)],forms=['condition'])
    change(28, '补同一目录表头及跨页表头；整个分类表合为一个完整单元。明确固定参考目录，不宣称最终行政等级。',
           question='工业互联网企业网络安全分类分级的参考行业目录中，电子设备制造、轻工、纺织和食品分别属于哪一类行业？',
           bundles=[b('网络安全分类分级管理指南',55,56,64,71,72,75,76,77,78,79,80)],
           scope='只查询固定库中征求意见稿参考目录的行业类别；不是企业级别或当前最终行政认定。')
    change(29, '删除独立的诚信记录职业用途问项，保留高风险科研项目失败免责条件。',
           question='承担探索性强、风险高的科学技术研究开发项目，最终未能完成时，科研人员在什么条件下可予以免责？',
           points=['项目属于探索性强、风险高的科学技术研究开发项目',
                   '原始记录等能够证明科研人员已经履行勤勉尽责义务',
                   '即使履行勤勉尽责义务仍不能完成项目的，予以免责'],bundles=[original_bundle(29,'g2')],forms=['condition'])
    change(30, '删除无共同任务的计分公式，避免T5源内疑误进入必答；保留分类清单且显式非穷尽。记录旧0004部分考点交集，不误判整题重复。',
           question='工业互联网平台解决方案的场景标签，按应用环节列举了哪些类别？这些列举是否穷尽所有类别？',
           points=['研发设计','生产制造','质量管控','运营管理','设备管理','采购销售','仓储物流',
                   '安全生产','节能减排','产融合作','产教合作','公共服务','原文使用包括但不限于，所列类别不是穷尽清单'],
           bundles=[original_bundle(30,'g1')],scope='仅问5.2.2.1所列类别与非穷尽限定，不要求每类全部服务示例，不问服务商公式。')
    change(32, '把角色清单和职责重复计分改为每角色一项，角色与其责任在同一点中识别。',
           points=['业务系统控制者：确保业务系统发起的数据库访问安全合规，并对业务数据处理目的与合法性负责',
                   '数据库管理责任者：其管理组织决定数据存储和处理方式，是运行环境与存储安全的首要责任方',
                   '运维系统责任者：其所属组织决定运维策略，是防范误操作、权限滥用的首要责任方'])
    change(33, '取消行业、民生与网络三段拼接，保留应对带宽时延需求的网络智算服务任务，procedure改fact。',
           question='为提升网络智算服务能力并适应智能体、具身智能的上行带宽和时延需求，政策对专线类型、光纤接入和5G-A部署提出了哪些措施？',
           points=['丰富OTN、PON、IP专线等多种类型，实现业务快速开通和灵活调度',
                   '提升光纤接入网上行带宽配置','推进支持大上行能力的5G-A网络部署',
                   '优化网络体验，降低网络端到端时延'],bundles=[original_bundle(33,'g3')],forms=['fact'])
    change(34, '撤回身份认证可证明设备计数准确性的关联；收窄到工控身份认证，不再引用属于不同一级指标的表格行。原两行列本身无错。',
           question='工业控制系统在登录和资源访问时，对身份认证、权限分配、口令及认证证书保护有哪些要求？',
           points=['工业主机登录、应用服务资源访问及工业云平台访问等过程中使用身份认证管理',
                   '关键设备、系统和平台访问采用多因素认证','分类设置账户权限，按最小特权原则分配',
                   '强化工控设备、SCADA软件和工业通信设备等的登录账户及密码，避免默认或弱口令',
                   '定期更新口令','保护身份认证证书信息，不在不同系统和网络环境下共享'],
           bundles=[original_bundle(34,'g1')],forms=['condition'])
    change(36, '把包括改为例如及等，保留非穷尽示例；题面明确要求类型和示例，分别评分类型及示例。',
           question='工业设备上云“领跑者”计划重点推动哪四类设备上云用云？每类有哪些列举的例子？',
           points=['高耗能流程行业设备，例如工业窑炉、工业锅炉、石油化工设备等',
                   '通用动力设备，例如柴油发动机、大中型电机、大型空压机等',
                   '新能源设备，例如风电、光伏等','智能化设备，例如工程机械、数控机床等'],
           scope='问四种重点设备类别和原文所举例子；例子不是各类设备的封闭目录。')
    change(37, '去掉著作权主体与作品保护范围混用；只保留法定赔偿任务，补制止侵权合理开支。',
           question='著作权侵权中，权利人实际损失、侵权人违法所得及权利使用费都难以计算时，法院可依什么确定赔偿、金额范围是多少？制止侵权的合理开支是否应计入？',
           points=['人民法院根据侵权行为的情节确定赔偿','法定赔偿为500元以上500万元以下',
                   '赔偿数额还应包括权利人为制止侵权行为支付的合理开支'],
           bundles=[b('著作权法_',219,220,221)],forms=['condition'])
    change(38, '删除与旧0006重复的平台跨行业能力；比较工业企业和平台企业在同一数据分类任务中的模式与维度，补第6条。',
           question='工业企业和平台企业梳理工业数据分类清单时，各应结合什么模式、考虑哪些共同因素？两类企业分别列举了哪些数据域作为分类维度？',
           points=['工业企业结合生产制造模式，平台企业结合服务运营模式',
                   '分析梳理业务流程和系统设备','考虑行业要求、业务规模、数据复杂程度等实际情况',
                   '对数据进行分类梳理和标识，形成企业工业数据分类清单',
                   '工业企业维度包括但不限于研发、生产、运维、管理、外部数据域',
                   '平台企业维度包括但不限于平台运营、企业管理数据域'],
           bundles=[b('工业数据分类分级指南',8),b('工业数据分类分级指南',9),b('工业数据分类分级指南',10)],
           forms=['comparison','procedure'],span='within_document',difficulty='medium',mappings=[['g1']]*4+[['g2'],['g3']],
           multi='第5条提供共同分类流程及模式差异，第6、7条提供各自维度，同一分类清单任务需结合三条。')
    change(40, '补199灵活组网第4项，同一连续1—4枚举合组single_unit；按独立特征拆分评分。原不通生产域间按上下文归纳，不改证据。',
           points=['扁平化：打破车间级、现场级分层组网，使智能机器直接横向互联',
                   '扁平化：IT和OT部分功能融合或通过工业云平台实现管理控制扁平化，实时控制功能下沉到智能机器',
                   '以太网/IP化：现场总线逐步由工业以太网替代，通用标准工业以太网逐步替代私有工业以太网',
                   '以太网/IP化：实现控制数据与信息数据同口传输','以太网/IP化：用IPv6解决大量IP装备接入',
                   '无线化：用于信息采集、非实时控制、厂内信息化，并向工业实时领域渗透，作为有线控制网络补充或替代',
                   '灵活组网：按需求灵活重构生产域，智能机器在生产域间迁移转换、域内即插即用',
                   '灵活组网：网络层资源可编排，SDN是实现方式之一'],
           bundles=[b('园区网络白皮书',*range(192,200))],forms=['fact'],
           scope='固定白皮书提出的四方面改进方向；不要求所有无线制式、HMI或标准组织举例。')
    change(41, '补备案公示并纠正安全评估的属性条件落在新产品/应用/功能，而非提供者所有新功能。第19条合为完整单元。',
           question='深度合成应用上架和运营时，分发平台应怎样核验并处理违规？具有舆论属性或社会动员能力的服务提供者须办理哪些备案手续、如何公示？开发上线哪类新产品、新应用、新功能时须安全评估？',
           points=['分发平台落实上架审核、日常管理、应急处置等安全管理责任',
                   '核验深度合成应用的安全评估、备案等情况',
                   '违规时及时采取不予上架、警示、暂停服务或下架等措施',
                   '具有舆论属性或社会动员能力的服务提供者按算法推荐管理规定履行备案及变更、注销手续',
                   '完成备案后，在对外服务网站、应用程序等显著位置标明备案编号','提供备案公示信息链接',
                   '开发上线具有舆论属性或社会动员能力的新产品、新应用、新功能时，按国家规定开展安全评估'],
           bundles=[b('深度合成管理规定',24),b('深度合成管理规定',42,43,44),b('深度合成管理规定',45)],
           forms=['condition','procedure'],span='within_document',difficulty='medium',mappings=[['g1']]*3+[['g2']]*3+[['g3']],
           multi='同一深度合成应用上架合规任务，需联合分发平台核验、提供者备案公示、产品属性触发的安全评估，分属第13、19、20条。')
    change(42, '删除无比较基础的算法推荐问项，保留服务商经营能力四维评价。按每维内容分组评分，去掉总述重复分。',
           question='工业互联网平台服务商经营能力评价关注哪四个方面？每个方面列举了哪些考察内容？',
           points=['运营能力：从业时间、员工数量、工业互联网业务收入、市场份额等',
                   '盈利能力：近三年营业收入和利润、解决方案和产品投资回报、技术经济性等',
                   '商业模式：融资计划、融资水平、投资并购、商业推广模式等',
                   '发展空间：营收与利润增长、未来市场空间等'],bundles=[original_bundle(42,'g1')])
    change(44, '保留两个实质分类单元，对七个类别分别计分，避免一个完整三类/四类清单作为大评分点。',
           points=['总体与基础共性：术语和定义','总体与基础共性：总体架构','总体与基础共性：密码应用',
                   '终端与设施：车载设备网络安全','终端与设施：车端网络安全','终端与设施：路侧通信设备网络安全',
                   '终端与设施：网络设施与系统安全'],span='within_document',
           mappings=[['g1']]*3+[['g2']]*4,multi='两类标准是体系中不同实质分支，各自有独立构成规定；共同分类查询需要两者。',
           reference='总体与基础共性包括术语和定义、总体架构、密码应用等3类；终端与设施网络安全包括车载设备网络安全、车端网络安全、路侧通信设备网络安全、网络设施与系统安全等4类。')
    change(45, '仍因旧0013已覆盖而排除；收窄为父母监护人同意义务，避免单点答案被当成完整未成年人信息处理清单。',
           question='拟人化互动服务提供者处理不满14周岁未成年人个人信息时，关于取得父母或者监护人同意有什么规定？',forms=['condition'],
           scope='只问父母/其他监护人同意义务；不声称是全部未成年人信息处理条件；旧题已覆盖此核心考点。')

    change(46, '删除错配的同步JSON示例及截断签名；通过一天128条的参数场景考异步周期字段，不强求任意JSON字符串。记录旧0008仅发送时机部分重叠。',
           question='结构化数据采用异步式动态标签的设定时间周期模式时，约定周期为1天，该周期内向同一应用场景下同一接收方发送128条数据。标签的时间间隔、起始时间、数据规模、传输校验和发送时机应怎样设置？',
           points=['标签生成周期为86400秒，时间戳字段中的时间间隔设为00086400',
                   '时间戳中的起始时间取该周期内第一条数据的发送起始时刻',
                   '按同一应用场景下同一接收方统计条数，本场景数据规模为128条',
                   '传输校验字段取该周期最后一条数据的杂凑值',
                   '该周期最后一条数据传输完毕后再发送标签'],
           bundles=[original_bundle(46,'g1')],forms=['procedure','condition'],
           scope='只考异步设定时间周期模式，假设条数已知；不要求JSON、不用同步示例、不要求标识或签名字面量。')
    change(48, '删除制定依据独立导航问项；补82—89完整表头与首子行，并视觉核对第8页四方面。纠正初审86块首词和草案作为致命缺陷的表述。',
           question='工业互联网企业网络安全分类分级的参考评定表中，企业应用工业互联网的程度按哪四个方面综合判定？',
           points=['企业两化融合程度','互联互通程度','综合集成程度','数据分析利用程度'],
           bundles=[b('网络安全分类分级管理指南',82,83,86,87,88,89)],
           scope='查询固定库征求意见稿参考评定表中的四方面；只问方面名称，不问最新规则、实际等级或每方面全部子要素。')
    change(49, '把无锚定当前改为人工智能+行动意见发布时国家发展改革委的背景判断，避免跨时点外推。',
           question='在解读“人工智能+”行动意见出台背景时，国家发展改革委认为人工智能技术正从哪个阶段迈向哪个阶段？',
           points=['从试验探索迈向价值创造阶段'],
           scope='问固定政策解读发布时的评述，不将其当作任意时点的最新事实。')
    change(50, '删除持续改进与行业分类的虚假绑定，保留监测流程；按七步骤分别评分并保留顺序。',
           question='开展工业互联网平台监测分析，主要工作流程按什么顺序推进？',
           points=['先明确实施目标','确定样本范围','构建指标体系','制定技术方案','开展数据对接',
                   '分析监测数据','推动持续改进'],bundles=[original_bundle(50,'g1')],forms=['procedure'],
           scope='只问流程顺序和步骤名称，不问附录行业分类或每个步骤全部操作细节。')
    change(51, '原13点均有引用；缺陷归因为人员管理g项遗漏，不是原点无证据。补153外包人员四项管控，拆技术与人员独立措施。',
           points=['访问控制：按最小权限限制接口查询范围和数量',
                   '访问控制：加密存储配置文件、应用代码及第三方配置中心中的数据库连接账号密码',
                   '访问控制：按来源IP及应用身份实施精细化控制','访问控制：动态连接IP白名单，只允许名单内应用联网访问',
                   '访问控制：采用基于角色或属性的机制，对查询、修改、删除等实施行级或列级细粒度权限控制',
                   '访问控制：通信会话超时重认证','访问控制：查询操作绑定会话令牌','访问控制：查询请求完整性校验',
                   '访问控制：连接超时自动断开','访问控制：失败登录阈值控制及锁定',
                   '访问控制：特权权限分离，至少设系统管理员、安全管理员、安全审计员',
                   '系统管理员负责系统配置、运行状态监控等','安全管理员负责访问控制、密文传输等安全维护',
                   '安全审计员负责审计开关控制、审计数据查看和管理',
                   '人员管理：建立岗位责任制，明确生命周期职责及操作权限边界',
                   '人员管理：严格权限审批分配，按最小权限分配管理、访问、运维权限，禁止过度授予',
                   '人员管理：定期审查调整权限，按岗位职责变动、离职转岗及时更新',
                   '人员管理：对关键岗位、数据库管理员及可访问敏感数据人员进行岗前背景审查和在岗定期复审',
                   '人员管理：操作人员身份和行为唯一标识且不可否认','人员管理：推广多因素、动态令牌或生物特征等强身份鉴别',
                   '人员管理：高风险操作实行双人复核与审批','人员管理：对高风险操作全过程审计跟踪',
                   '第三方外包人员：签订保密协议','第三方外包人员：限制访问范围',
                   '第三方外包人员：操作行为全程监控','第三方外包人员：审计日志独立留存'],
           bundles=[b('数据库联网安全要求',82,*range(83,91)),b('数据库联网安全要求',145,*range(146,154))],
           forms=['condition'],span='within_document',difficulty='medium',mappings=[['g1']]*14+[['g2']]*12,
           multi='同一数据库权限管理任务，5.2技术控制与6.3人员生命周期/外包管控互相补充，须覆盖两个必要单元。',
           scope='完整回答5.2访问控制和6.3人员管理条目；第6.2外包机构准入合同要求是另一主题，题面不扩至所有数据库合规。')
    change(53, '根密钥操作及预期结果逐项计分；密码证书检查与成功结果合为一事实，颁发机构、标准等级分别判定。',
           points=['根密钥：审查技术方案，检查是否预置受硬件保护的根密钥',
                   '根密钥：把经该根密钥加密的数据密文复制到其他设备，验证不能解密',
                   '根密钥：尝试修改根密钥','根密钥：尝试导出根密钥明文',
                   '根密钥预期：芯片预置受硬件保护的根密钥','根密钥预期：经根密钥加密数据不能在其他设备解密',
                   '根密钥预期：无法修改或导出根密钥',
                   '密码机制：检查研制单位能否出示认证证书，预期能够出示',
                   '认证证书由国家密码管理局商用密码检测认证中心颁发',
                   '认证证书符合GM/T 0008—2012安全等级1或更高等级要求'],
           bundles=[original_bundle(53,'g1'),original_bundle(53,'g2')],forms=['procedure'],span='within_document',difficulty='medium',
           mappings=[['g1']]*7+[['g2']]*3,multi='同一芯片密码安全测评中的硬件根密钥隔离与密码技术认证两个对象，检查内容互不替代。',
           scope='固定规范的根密钥和密码机制测评；标准及机构名称按该版本引用，不声称为当前所有产品的最新认证规则。')
    change(54, '把农业装备、能力和管理等独立措施拆分；育种与支持应用都是并列动作，撤回初审将第一点说成目的扩展的表述。',
           points=['加快人工智能驱动的育种体系创新','支持种植、养殖等农业领域智能应用',
                   '发展智能农机、农业无人机、农业机器人等智能装备',
                   '提高生产和加工工具的智能感知、决策、控制、作业能力',
                   '强化农机农具平台化、智能化管理','加强人工智能在农业生产管理、风险防范等领域应用',
                   '帮助农民提升生产经营能力和水平'])
    change(55, '同一5.2.1.2枚举三组归一；删除题面未问的通用型定义及行业型定义必答分，只保留两类名称与标准范围。',
           points=['按服务行业领域分为通用型解决方案和行业型解决方案',
                   '行业型分类表中的行业名称和分类标准应符合GB/T 4754—2017表1门类B、C、D规定'],
           bundles=[b('解决方案分类方法',77,78,79)])
    change(57, '删除题面已给目的评分点，condition改comparison/fact并锚定新一代规划近期远期框架。',
           question='新一代人工智能发展规划的安全监管框架，对近期与远期的影响关注重点分别是什么？',
           points=['近期重点关注对就业的影响','远期重点考虑对社会伦理的影响'],forms=['comparison','fact'],
           scope='固定规划中近期、远期的框架，不等同当前时点的风险排名。')
    change(58, '保留重复排除；同一联网企业分级小节合为一组，纠正最终等级问法及重复计分，仅作为已覆盖诊断变体。',
           question='按工业互联网企业网络安全分类分级参考指南，属于三类行业的规模以上联网工业企业初步评分75分，单按分数属于几级？特殊规则又原则上如何判定？',
           points=['75分按60分及以上、小于80分的区间属于二级',
                   '三类行业的规模以上联网工业企业原则上为三级，不能只按75分区间作最终判断'],
           bundles=[b('网络安全分类分级管理指南',21,22,23,24)],forms=['condition'],
           scope='固定征求意见稿参考规则的诊断变体，非实际最终行政定级；仍与旧0010重复，排除选用。')
    change(59, '批准与非经批准不得提供合为一个必答点；主管机关处理外国请求的法源不属于企业题面问项。',
           points=['须经中华人民共和国主管机关批准；非经批准，境内组织、个人不得向外国司法或执法机构提供存储于境内的数据'],
           forms=['condition'],scope='只考境内公司面对外国司法请求时的主管机关批准前提，不将请求本身或互惠原则当作公司直接授权。')
    change(60, '删除单独目标性评分点；与0019修订后的场景制度/知识产权/标准主题消解同核心生态清单重复。',
           question='在“人工智能+”行动的产业生态发展解读中，提出了哪些推动产业、服务商、智能原生企业和开源生态的建设措施？',
           points=items[59]['answer_points'][:7],scope='问固定解读的产业生态建设行动；国际影响力作为开源发展的目标说明，不独立计必答分。',
           reference='推动产业全要素智能化发展；培育人工智能应用服务商；打造人工智能应用服务链；大力发展智能原生技术、产品和服务体系；培育智能原生企业；加强开源生态建设；支持优质开源项目发展，并提升国际影响力。')

    return items, notes


def reconciliation():
    # The attachment contains repeated table rows and a truncated ending; only
    # reconcile supplied, verifiable claims, never invent the missing prose.
    return [
        {'ids':['batch_001-0004'],'judgment':'采纳并纠正','detail':'两个来源内容不冗余。初审主题相关的表述不充分，multi_source=false撤回。关键词独占只能证明不同内容，仍须题面形成共同任务；已重写园区建设任务，补271。'},
        {'ids':['batch_001-0006','batch_001-0008','batch_001-0031','batch_001-0044','batch_001-0060','batch_001_supplement_03-0002'],'judgment':'采纳','detail':'统一完整法条/小节/连续枚举为single_unit、一个证据组；0006原labels=true口径不一致。分组不按PDF分页或解析块数决定。'},
        {'ids':['batch_001-0032'],'judgment':'扩展同一标准','detail':'同一完整分类指导目录表原分成两组亦合为single_unit；补必要表头及跨页表头，分类映射不变。'},
        {'ids':['batch_001-0034','batch_001-0050'],'judgment':'采纳关联但不整题排除','detail':'分别与旧0004运营管理、旧0008异步发送时机部分重叠，保存关系。新任务分别考12类清单/非穷尽和周期字段配置，旧题不能完整覆盖。'},
        {'ids':['batch_001-0006','batch_001-0035','batch_001-0049','batch_001_supplement_03-0002'],'judgment':'采纳','detail':'duplicate_group写入修订候选、导入结果及CSV；duplicate_groups.json记录旧金标准成员ID。旧发布文件不改。'},
        {'ids':['batch_001-0002'],'judgment':'采纳','detail':'纠正合并列覆盖范围为11—99；补144表头。答案13组行业映射本身不变。'},
        {'ids':['batch_001-0015'],'judgment':'采纳','detail':'合作作品截止规则在原answer_points中存在，缺在reference正文；初审措辞修正。实质修订明确视听作品，不再按自然人/法人分支套期限。'},
        {'ids':['batch_001-0017'],'judgment':'采纳','detail':'总体价值原则题应fact，不用condition；题面收窄，避免基本要求被理解为完整换脸上线义务。'},
        {'ids':['batch_001-0020'],'judgment':'采纳','detail':'18块跨10/11条；新一级证据引用精确从第11条开始的子串，基础原则与等级理由分别映射。原abstain理由撤回。'},
        {'ids':['batch_001-0023'],'judgment':'采纳纠正初审表述','detail':'原scope_note没有原文提供措辞，撤回该归因；第29条及30条报告内容漏项仍成立，修订补齐并限定风险管理任务。'},
        {'ids':['batch_001-0024'],'judgment':'采纳范围遗漏提示；以收窄解决','detail':'原问全套拟人化事件措施须考虑第24条，初审漏记；修订只保留工控系统事中处置，拟人化条款不再在问题范围。'},
        {'ids':['batch_001-0028'],'judgment':'采纳并区分归纳与原文','detail':'230已含平台化架构融合机制，未引231不单独构成原题错误；改题明确只问该机制。原文生产过程终的为源内表述，引用不改、答案不逐字复述该疑误。'},
        {'ids':['batch_001-0034'],'judgment':'原PDF核验支持','detail':'物理第9页T5确为生态能力指标权重，与后续T5二级指标得分加权计算存在源内不一致，不是抽取错误。已删除该公式问项，不以私自修源作为证据。'},
        {'ids':['batch_001-0038'],'judgment':'采纳纠正归因','detail':'两块列归属本身无错，属不同一级指标且上下文不完整；撤回列混合说法。修订仅考身份认证，不再声称其证明计数准确。'},
        {'ids':['batch_001-0040'],'judgment':'采纳','detail':'包括可读成封闭例子列表，改为例如及等，保持示例非穷尽。'},
        {'ids':['batch_001-0052'],'judgment':'采纳','detail':'86块首词为根据企业信，右列才是企业两化融合程度；补完整表格并看PDF第8页。草案版本是适用范围提示，不能一律作致命来源错误。旧金标准未标草案的口径另记录，不在本轮改旧发布。'},
        {'ids':['batch_001-0056'],'judgment':'采纳','detail':'原13评分点有引用；问题在漏人员管理g项及粒度。撤回原点无证据的归因，补外包人员4项管控。'},
        {'ids':['batch_001-0059'],'judgment':'采纳','detail':'育种创新和支持应用是并列措施，不是目的扩展；拆独立动作，保留帮助农民提升能力。'},
        {'ids':['batch_001-0060'],'judgment':'采纳','detail':'两类定义均未被原题问及；删除额外定义必答，仅保留名称和行业型分类标准，三组归一。'},
        {'ids':['batch_001-0025'],'judgment':'采纳治理说明','detail':'初审corpus_search=false本身不是漏审承诺；本轮保留的纠错题重新完成固定全库例外搜索并记录，才设置true。'},
    ]


def boundary_search(corpus, item):
    import re
    # Keep historical mixed-retrieval hits separately; source-grounded edits
    # require current searches and the current applicable clauses, not a stale
    # generation search as the sole boundary justification.
    terms = ['算法安全主体责任','诱导用户沉迷','不适用','免除','豁免']
    records = []
    for d in corpus.documents.values():
        for block in d['blocks']:
            text = re.sub(r'\s', '', block['text'])
            hits = [t for t in terms if t in text]
            if hits and ('算法' in text or '算法推荐管理规定_' in d['source_path']):
                records.append({'doc_id':d['doc_id'],'source_path':d['source_path'],
                    'document_sha256':d['sha256'],'block_id':block['block_id'],
                    'matched_terms':hits,'text':block['text']})
    return {'id':item['id'],'method':'NFKC-free whitespace-normalized literal scan plus clause/context review',
        'document_count':len(corpus.documents),'corpus_identity':corpus.identity(),'queries':terms,
        'records':records,'related_articles_read':{'algorithm_recommendation':[9,18,19,55]},
        'decision':'第2条另有法律行政法规的适用保留已核对；固定库未提供不诱导沉迷即可免除主体责任的例外。第7、8条的正面义务支持纠错，不以未命中证明现实不存在其他法源。',
        'search_scope':'fixed benchmark corpus only','reviewer':REVIEWER,'authorization':AUTH}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--claude-review', required=True)
    args = parser.parse_args()
    out = Path(args.output)
    if out.exists():
        raise FileExistsError(f'Refusing to overwrite revision: {out}')
    corpus = Corpus('data/processed/baseline_v1')
    original = read_jsonl(SOURCE / 'candidates.jsonl')
    first = read_jsonl(SOURCE / 'agent_review_v1/reviewed.jsonl')
    gold = read_jsonl('data/benchmark/trial30_final_v1/gold.jsonl')
    old_audits = read_jsonl(SOURCE / 'agent_review_v1/audit.jsonl')
    assert len(original) == len(first) == 60
    protected = {str(SOURCE/name):sha for name,sha in json.loads((SOURCE/'artifact_hashes.json').read_text(encoding='utf-8')).items()}
    for folder in [SOURCE/'agent_review_v1',Path('data/benchmark/trial30_final_v1')]:
        protected.update({str(p):file_hash(p) for p in folder.rglob('*') if p.is_file()})
    assert all(file_hash(p)==sha for p,sha in protected.items())
    candidates, notes = revise(corpus, original)
    groups = [
        {'name':'dup-public-network-encryption-key-storage','members':[original[4]['id'],'trial_v3-pilot_v2-0016']},
        {'name':'dup-ai-government-service-mode','members':[original[30]['id'],'trial_v3-pilot_v2-0025']},
        {'name':'dup-under14-companion-data-consent','members':[original[44]['id'],'trial_v3-pilot_v2-0013']},
        {'name':'dup-network-enterprise-score-industry-grade','members':[original[57]['id'],'trial_v3-pilot_v2-0010']},
    ]
    group_by_id = {identifier:g['name'] for g in groups for identifier in g['members']}
    rejected_ids = {original[n-1]['id'] for n in [5,31,45,58]}
    assert rejected_ids == {i['id'] for i in first if i['review_status']=='rejected'}
    changed = []
    stamp = now()
    for n,(i,old) in enumerate(zip(candidates,original),1):
        content_changed = content_fingerprint(i)!=content_fingerprint(old)
        if content_changed:
            for key in ['semantic_precheck','boundary_precheck','machine_notes']:
                i.pop(key,None)
            # Original chunks/scenario remain generation history, not new support.
            i['generation_context_provenance'] = {'original_node_ids':old.get('node_ids',[]),
                'original_chunk_ids':old.get('chunk_ids',[]),'status':'historical generation context; use revised evidence_groups for support'}
        i.update(review_status='pending',reviewer='',review_notes='',review_checks={},
            duplicate_group=group_by_id.get(i['id'],''),revision_note=notes.get(n,'原题内容与证据复核后保留。'),
            source_id=old['id'],source_candidate_sha256=digest(old),
            revision_provenance={'method':'source_grounded_revision','author':'Codex',
                'authorization':AUTH,'human_per_item_review_claimed':False,'source_batch':str(SOURCE),
                'source_first_pass_sha256':digest(first[n-1]),'created_at':stamp},
            evidence_audit=evidence_audit(i,corpus))
        if i['expected_behavior'] != 'answer':
            # Existing mixed retrieval retained for inspection; current literal
            # scan is in boundary_search_audit.json and provenance below.
            i['corpus_search'] = copy.deepcopy(old.get('corpus_search',[]))
            i['corpus_search_provenance'] = {'historical_mixed_search_source':old['id'],
                'current_search_file':'boundary_search_audit.json','current_reviewed_at':stamp}
        validate_item(i,corpus)
        fields=[k for k in ('user_input','reference','answer_points','evidence_groups','answer_form','evidence_span',
            'difficulty','expected_behavior','scope_note','point_evidence','multi_source_reason','duplicate_group') if old.get(k)!=i.get(k)]
        changed.append({'n':n,'id':i['id'],'source_candidate_sha256':digest(old),
            'source_content_sha256':content_fingerprint(old),'revised_content_sha256':content_fingerprint(i),
            'content_changed':content_changed,'changed_fields':fields,
            'changes':{k:{'before':old.get(k),'after':i.get(k)} for k in fields},
            'revision_note':i['revision_note'],'first_review_status':first[n-1]['review_status'],
            'first_review_rationale':old_audits[n-1]['rationale'],'authorization':AUTH,
            'self_review_note':'修改后按题面范围、最小必要点、逐项支持、漏项、实质证据单元、来源质量及旧30/同批重复复核；不是独立人工审核。'})
    audit_search=boundary_search(corpus,candidates[22])
    boundary_docs={r['doc_id'] for r in audit_search['records']}
    assert boundary_docs
    # Dedup check with old anchors carrying groups in memory only; original
    # release bytes and item objects are never mutated.
    reference_items=copy.deepcopy(gold)
    for x in reference_items:
        if x['id'] in group_by_id:
            x['duplicate_group']=group_by_id[x['id']]
    eligible=[i for i in candidates if i['id'] not in rejected_ids]
    _,links=duplicate_clusters(reference_items+eligible)
    eligible_ids={i['id'] for i in eligible}
    links=[pair for pair in links if any(identifier in eligible_ids for identifier in pair)]
    if links:
        raise ValueError(f'Remaining eligible duplicate links: {links}')
    out.mkdir(parents=True)
    shutil.copyfile(args.claude_review,out/'claude_review_submitted.txt')
    write_jsonl(out/'candidates.jsonl',candidates)
    write_jsonl(out/'reconciliation.jsonl',reconciliation())
    write_json(out/'duplicate_groups.json',{'groups':groups,'old_release_modified':False,
        'application':'Assign these groups to copied old reference items when constructing a future pool; never rewrite frozen gold. Rejected variants must not enter the pool.'})
    write_json(out/'boundary_search_audit.json',audit_search)
    export_review(candidates,out/'review',corpus)
    with (out/'review/review.csv').open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream); fields=reader.fieldnames; rows=list(reader)
    for n,(row,i) in enumerate(zip(rows,candidates),1):
        checks={k:True for k in (*REVIEW_CHECKS,*CONTENT_CHECKS)}
        checks['corpus_search']=i['expected_behavior']!='answer'
        is_rejected=i['id'] in rejected_ids
        checks['duplicates']=not is_rejected
        row.update(review_status='rejected' if is_rejected else 'approved',reviewer=REVIEWER,
            review_notes=i['revision_note']+'\n[来源] 用户明确授权的原文修订及同一agent自检，不声称用户逐题审核或独立复审。'+
                ('\n[排除] 核心答案已由旧30题覆盖，duplicate_groups.json给出成员关系。' if is_rejected else ''),
            review_checks=json.dumps(checks,ensure_ascii=False))
    with (out/'review/review.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fields);writer.writeheader();writer.writerows(rows)
    reviewed=import_review(candidates,out/'review/review.csv',corpus)
    for i,log,candidate in zip(reviewed,changed,candidates):
        i['review_provenance']={'method':'user_authorized_revision_self_check','reviewer':REVIEWER,
            'authorization':AUTH,'human_per_item_review_claimed':False,'independent_reviewer':False,
            'scope':'candidate revision and self-check; no freeze, no split assignment','audit_file':'revisions.jsonl'}
        if i['review_status']=='approved':
            i['approval_provenance']={**i['review_provenance'],'at':i['reviewed_at']}
        validate_item(i,corpus,approved=i['review_status']=='approved')
        log.update(final_review_status=i['review_status'],review_checks=i['review_checks'],
            evidence_audit=evidence_audit(i,corpus),revised_candidate_sha256=digest(candidate))
    write_jsonl(out/'reviewed.jsonl',reviewed)
    write_jsonl(out/'approved.jsonl',[i for i in reviewed if i['review_status']=='approved'])
    write_jsonl(out/'rejected.jsonl',[i for i in reviewed if i['review_status']=='rejected'])
    write_jsonl(out/'revisions.jsonl',changed)
    # Leave the source-bound editable CSV intact; export the imported state to
    # a separate reading packet, so hashes/CSV/JSON never disagree.
    export_review(reviewed,out/'review_after_import',corpus)
    def coordinates(item):
        return {(e['doc_id'],e['block_id']) for g in item['evidence_groups'] for bundle in g['alternatives'] for e in bundle}
    overlaps=[]
    for old,new in zip(original,candidates):
        for reference in gold:
            before=coordinates(old)&coordinates(reference)
            after=coordinates(new)&coordinates(reference)
            if before or after:
                overlaps.append({'candidate_id':old['id'],'reference_id':reference['id'],
                    'shared_blocks_before':[{'doc_id':d,'block_id':bid} for d,bid in sorted(before)],
                    'shared_blocks_after':[{'doc_id':d,'block_id':bid} for d,bid in sorted(after)],
                    'same_core':bool(group_by_id.get(old['id'])) and group_by_id.get(old['id'])==group_by_id.get(reference['id']),
                    'note':'证据交集不自动等于同核心；结论以same_core组及题意为准。'})
    write_json(out/'duplicate_relations.json',{
        'same_core':groups,'partial_overlap':[
            {'candidate_id':original[29]['id'],'reference_id':'trial_v3-pilot_v2-0004','basis':'原引用同解决方案分类87块。修订题问12类清单与非穷尽，不问运营管理服务内容。','same_core':False},
            {'candidate_id':original[45]['id'],'reference_id':'trial_v3-pilot_v2-0008','basis':'标签223块共享发送时机。修订题还问周期、起始时刻、128条规模、末条杂凑；旧题仅对比发送时机。','same_core':False},
            {'candidate_id':original[16]['id'],'reference_id':original[59]['id'],'basis':'原产业生态清单交集；0019已改为场景制度、知识产权、标准，不再考生态措施。','same_core_after_revision':False}],
        'all_shared_block_relations_before_and_after':overlaps,'automatic_remaining_eligible_links':links,
        'semantic_review':'按题意与最小必要答案核对旧30题及同批题；共享表格或相邻原文不自动等于同核心。'})
    summary={'status':'source_grounded_revision_self_check_complete','total':len(reviewed),
        'review_status':dict(Counter(i['review_status'] for i in reviewed)),
        'content_changed':sum(r['content_changed'] for r in changed),
        'original_needs_revision_resolved':sum(r['first_review_status']=='needs_revision' and r['final_review_status']=='approved' for r in changed),
        'labels':{'evidence_span':dict(Counter(i['evidence_span'] for i in reviewed if i['review_status']=='approved')),
                  'expected_behavior':dict(Counter(i['expected_behavior'] for i in reviewed if i['review_status']=='approved')),
                  'category':dict(Counter(i['category'] for i in reviewed if i['review_status']=='approved'))},
        'validation':{'valid_items':len(reviewed),'csv_import_round_trip':True,
            'no_eligible_automatic_duplicates_with_old30':not links,'original_candidate_first_review_and_release_unchanged':True},
        'authorization':AUTH,'review_method':'user_authorized_revision_self_check',
        'human_per_item_review_claimed':False,'independent_reviewer':False,'no_freeze_performed':True,'external_llm_calls':0}
    assert all(file_hash(p)==sha for p,sha in protected.items())
    assert all(r['final_review_status']!='needs_revision' for r in changed)
    write_json(out/'summary.json',summary)
    shutil.copyfile(SOURCE/'families.json',out/'families.json')
    source_manifest=json.loads((SOURCE/'manifest.json').read_text(encoding='utf-8'))
    write_json(out/'manifest.json',{**source_manifest,'status':summary['status'],'created_at':stamp,'schema_version':2,
        'corpus':corpus.identity(),'source_batch':str(SOURCE),'source_candidates_sha256':file_hash(SOURCE/'candidates.jsonl'),
        'source_first_review_sha256':file_hash(SOURCE/'agent_review_v1/reviewed.jsonl'),
        'claude_review_sha256':file_hash(args.claude_review),'authorization':AUTH,'script_sha256':file_hash(__file__),
        'protected_artifact_hashes':protected,'no_freeze_performed':True,
        'source_visual_checks':[
            {'path':'data/raw/technical/工业互联网企业网络安全分类分级管理指南（试行）.pdf','sha256':file_hash('data/raw/technical/工业互联网企业网络安全分类分级管理指南（试行）.pdf'),'physical_pages':[8],'result':'四方面及列归属已核'},
            {'path':'data/raw/technical/工业互联网平台服务商评价方法.pdf','sha256':file_hash('data/raw/technical/工业互联网平台服务商评价方法.pdf'),'physical_pages':[9],'result':'T5权重字样存在于原PDF，修订题不考该公式'}]})
    lines=['# 新增60题修订 v2','',
        f"原38道需修订题全部修改并自检；本版56道approved候选、4道重复rejected。{summary['content_changed']}道内容字段发生变化。",'',
        '审核来源：用户明确授权Codex根据Claude Code复核与自身判断修订、自检；approved表示该轮自检通过，不声称独立人工复审或用户逐题审阅。未冻结为金标准、未分配dev/holdout。', '',
        '原生成候选、agent_review_v1和已定稿30题均保持原字节。旧初审的误报、漏报及措辞在reconciliation.jsonl中纠正，不覆盖历史报告。附件重复表格和截断尾段原样保存，只处理已提供的可验证意见。','',
        '## 阅读入口','',
        '- `review_after_import/reading.md`：修订后全部题目、参考答案、评分点和逐字原文证据。',
        '- `review/review.csv`：本轮已填审核表，candidate_hash对应本版candidates.jsonl，可继续修改并导入新文件。',
        '- `reviewed.jsonl`：60题导入结果；`approved.jsonl`为56题可继续人工复核的候选；`rejected.jsonl`为4题重复诊断变体。',
        '- `revisions.jsonl`：逐题前后字段、源哈希、修订依据与自检记录。',
        '- `reconciliation.jsonl`：对Claude意见的判断及原初审纠正。',
        '- `duplicate_groups.json`：同核心组及旧题成员；`duplicate_relations.json`另记录部分证据重叠。',
        '- `boundary_search_audit.json`：本版0025纠错题的固定全库例外检索与条款核查。','',
        '## 适用范围与变化','',
        '同一完整法条、小节、连续枚举或分类表用single_unit和单组；跨页不自动变成多跳。同一任务的多个独立条款/分类分支仍可within_document。无关跨域双问收窄，不凑多跳配额。', '',
        '0004保留跨文档园区无线建设任务。0010由无依据纠错改为政策互联互通措施题；0015由不正确的版权澄清改为明确视听作品期限比较；0020由假设个案拒答改为完整潜在影响条件的分级题；0025保留纠错并完成本轮全库核验。因此本版边界题只有1道纠错，不能视作三种边界行为均已覆盖。','',
        '0034删除服务商计分公式问项，T5源内疑误保留在核查记录；0050改为异步周期字段配置场景，删除错配同步JSON；0056补外包人员g项；0044补灵活组网。分类分级源文件草案身份用固定材料范围说明，不一律当成内容错误。旧30题版本口径不在本次重写。','',
        '4道重复仍不选用，不计作新独立题。不会为保留60可用题而更换来源造新题；若后续要求新增60道可用题，需再补4道并审核。','',
        '## 逐题修订与状态','', '|序号|ID|状态|变更|','|---|---|---|---|']
    for r in changed:
        lines.append(f"|{r['n']}|{r['id']}|{r['final_review_status']}|{r['revision_note'].replace('|','/')}|")
    lines += ['','## 校验','',
        '全部60题经schema、字符区间、原文哈希、评分映射、标签及审核勾选校验；审核CSV实际导入成功。56个通过候选与旧30自动去重无剩余链，另按核心答案进行语义检查。机械校验不替代独立人工复审。', '',
        '## 重现','', '在仓库根目录、conda rag环境运行，输出目录必须尚不存在：','',
        '```powershell',
        'python scripts/revise_benchmark_batch001.py --output data/benchmark/batch_001_review60/revision_v2_replay --claude-review data/benchmark/batch_001_review60/revision_v2/claude_review_submitted.txt',
        '```','',
        '后续编辑review/review.csv时，以本版candidates.jsonl导入新reviewed文件并导出到新目录；不要使用原批候选导入本版审核表。']
    (out/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    shutil.copyfile(__file__,out/'assemble_revision.py')
    write_json(out/'artifact_hashes.json',{str(p.relative_to(out)):file_hash(p) for p in sorted(out.rglob('*')) if p.is_file()})
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
