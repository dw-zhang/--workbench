# -*- coding: utf-8 -*-
"""生成演示数据 seed_data.json（全部为虚构内容，不含任何真实人员/企业/单据信息）

用法: cd 仓库根目录 && python3 tools/gen_demo_seed.py
"""
import json
import datetime

T = datetime.datetime(2026, 11, 15, 9, 0, 0)


def iso(days, hours=0):
    return (T + datetime.timedelta(days=days, hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


PEOPLE = ['演示用户', '员工甲', '员工乙', '员工丙', '员工丁', '员工戊']

TASK = '151WfQT8N0hn6nsOMnmMqh'
SCHED = 'KHYNKWUOMqxe4rqJJFOl6t'
WAIT = 'DiQ0qiGK3ZejC7g3bc1NDk'
FILES = '8rrQVoUCjp1jWnVlVLkWIm'
DING = 'VogzQYVe0XppAYgBuOpS1s'
MEET = '8pG50L6vRsWi6v7P8YlGC7'
CAT = 'TWmt9f5r0XRGHDZBc7v9Wx'
APPS = 'OnTaihRfxXKvpltLhEXgEr'

task_names = [('处理服务工单 #1024', '高', '进行中', 'IT 运维'),
              ('整理月度资产台账', '中', '待处理', '资产管理'),
              ('评审新系统准入方案', '高', '进行中', '信息化项目'),
              ('更新机房巡检记录', '低', '已完成', 'IT 运维'),
              ('整理供应商比价表', '中', '待处理', '采购支持'),
              ('编写网络改造方案', '高', '进行中', '信息化项目'),
              ('清理到期域名与证书', '中', '已完成', 'IT 运维'),
              ('归档上季度合同扫描件', '低', '待处理', '档案管理')]

tasks = []
for i, (name, pri, st, proj) in enumerate(task_names):
    tasks.append({'任务名称': name, '优先级': pri, '截止时间': iso(i % 7 - 2, 6 + i),
                  '负责人': PEOPLE[i % len(PEOPLE)], '状态': st, '所属项目': proj,
                  '处理链接': 'https://example.com/task/%d' % (1000 + i),
                  '来源应用': '钉钉', '钉钉任务ID': 'demo-task-%04d' % (1000 + i)})

scheds = []
for i, (title, loc, who) in enumerate([
        ('IT 周例会', '线上会议', '演示用户'),
        ('机房设备巡检', '一号机房', '员工甲'),
        ('新系统上线评审', '3F 会议室', '演示用户'),
        ('供应商线上沟通', '视频会议', '员工乙'),
        ('月度资产盘点', '仓库', '员工丙')]):
    scheds.append({'日程事项': title, '开始时间': iso(i, 2 + i), '时长': 60,
                   '说明': '演示数据，可删除', '来源应用': '钉钉',
                   '事件ID': 'demo-event-%04d' % (2000 + i)})

waits = []
for i, (name, src, sender) in enumerate([
        ('演示用户 在群「IT 支持」里 @你：打印机离线怎么处理？', '群消息', '员工甲'),
        ('客户询问：账号权限多久能开好？', '钉钉消息', '员工乙'),
        ('请确认本周值班安排', '群消息', '员工丙'),
        ('这条工单需要你补充处理意见', '待办', '员工丁')]):
    waits.append({'事项名称': name, '来源': src, '发起时间': iso(-i, 3),
                  '状态': '等待回复', '备注': '',
                  '处理链接': 'https://example.com/msg/%d' % (3000 + i),
                  '消息ID': 'demo-msg-%04d' % (3000 + i),
                  '群名称': 'IT 支持' if src == '群消息' else '',
                  '发送人': sender})

files = []
doc_titles = ['IT 管理制度汇编', '网络拓扑图 v3', '资产领用流程', '准入申请模板',
              '机房巡检表', '软件白名单', 'VPN 使用说明', '离职交接清单']
for i, t in enumerate(doc_titles):
    files.append({'文件名称': t, '类型': '文档', '所属项目': 'IT 文档库',
                  '更新时间': iso(-i, 1), '大小': 10240 * (i + 1),
                  '更新人': PEOPLE[i % len(PEOPLE)],
                  '链接': 'https://example.com/doc/%d' % (4000 + i),
                  '来源应用': '钉钉', '知识库': '演示知识库',
                  '层级路径': '演示知识库/IT 文档库', '父节点ID': 'demo-node',
                  '节点ID': 'demo-doc-%04d' % (4000 + i), '子项数': 0,
                  '空间链接': 'https://example.com/space/1', '排序': i,
                  '层级': 1, '文档ID': 'demo-doc-%04d' % (4000 + i), '文档UUID': ''})

dings = []
for i, (title, typ, st) in enumerate([
        ('采购申请（显示器 2 台）', '采购', '审批中'),
        ('补卡申请', '考勤', '审批中'),
        ('年假申请', '请假', '已完成'),
        ('外部人员准入申请', '准入', '审批中'),
        ('付款申请（服务费）', '付款', '已完成'),
        ('加班申请', '考勤', '已完成')]):
    dings.append({'审批标题': title, '审批类型': typ, '发起人': PEOPLE[(i + 1) % len(PEOPLE)],
                  '审批状态': st, '发起时间': iso(-i - 1, 2),
                  '审批编号': 'DEMO-PROC-%04d' % (5000 + i), '审批来源': '我审批的',
                  '来源应用': '钉钉', '涉及金额': '是' if typ in ('采购', '付款') else '否',
                  '表单明细': '申请事由：演示数据\n数量：2\n预算金额：2400',
                  '审批流转': '演示用户 同意 2026-11-14 10:00'})

meets = []
for i, (topic, org) in enumerate([('IT 月度例会', '演示用户'), ('项目周会', '员工甲'),
                                  ('供应商方案沟通', '员工乙')]):
    meets.append({'会议主题': topic, '开始时间': iso(i - 1, 5), '时长': 60,
                  '地点': '线上会议', '组织人': org, '参会人': '、'.join(PEOPLE[:4]),
                  '状态': '已结束' if i == 0 else '待开始',
                  '纪要链接': 'https://example.com/minutes/%d' % (6000 + i),
                  '事件ID': 'demo-meet-%04d' % (6000 + i)})

cats = [{'分类名称': 'IT 运维', '所属模块': '任务', '颜色': '#2563eb', '排序': 1, '说明': '演示分类'},
        {'分类名称': '资产管理', '所属模块': '任务', '颜色': '#059669', '排序': 2, '说明': '演示分类'},
        {'分类名称': '信息化项目', '所属模块': '任务', '颜色': '#d97706', '排序': 3, '说明': '演示分类'}]

apps = [{'应用名称': '钉钉', '应用标识': 'dingtalk', '应用类型': '办公协同', '接入状态': '未接入',
         '接口地址': 'https://oapi.dingtalk.com', '企业ID': '', '应用ID': '', '应用密钥': '',
         '用户ID': '', '同步能力': '待办/审批/群消息/知识库', '最后同步': '', '说明': '在管理员控制台填写凭证'},
        {'应用名称': '飞书', '应用标识': 'feishu', '应用类型': '办公协同', '接入状态': '未接入',
         '接口地址': 'https://open.feishu.cn', '企业ID': '', '应用ID': '', '应用密钥': '',
         '用户ID': '', '同步能力': '审批', '最后同步': '', '说明': '可选'},
        {'应用名称': 'GitHub', '应用标识': 'github', '应用类型': '研发协同', '接入状态': '未接入',
         '接口地址': 'https://api.github.com', '企业ID': '', '应用ID': '', '应用密钥': '',
         '用户ID': '', '同步能力': 'Issue/PR', '最后同步': '', '说明': '可选'}]

out = {TASK: tasks, SCHED: scheds, WAIT: waits, FILES: files,
       DING: dings, MEET: meets, CAT: cats, APPS: apps}

with open('seed_data.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print('written', sum(len(v) for v in out.values()), 'records')
