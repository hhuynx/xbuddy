"""Knowledge, follow-up and deliverable interactions for the Python workbench."""
from __future__ import annotations

from datetime import datetime, timedelta

from nicegui import ui

from server.exports import export_report
from server.memory import KINDS
from server.models import FollowUpCreate, MemoryCreate, MemoryDecision, MemoryUpdate, ReportRevision
from server.services import ServiceError
from server.ui.presentation import SHANGHAI, claim_text, stamp

MEMORY_STATUS = {'active':'有效', 'pending':'等待确认', 'rejected':'已拒绝', 'expired':'已过期', 'deleted':'已删除', 'conflict':'依据有冲突', 'legacy_unverified':'待复核'}


class KnowledgeWorkbench:
    def selected_artifact(self, artifacts):
        if self.report_run_id != self.run_id:
            self.artifact_id, self.report_run_id = None, self.run_id
        artifact = next((a for a in artifacts if a['id'] == self.artifact_id), artifacts[-1])
        self.artifact_id = artifact['id']
        return artifact

    def select_artifact(self, event):
        if event.value and event.value != self.artifact_id:
            self.artifact_id = event.value
            self.render_report()
            with self.client:
                self.remember()

    def go_to_run(self, run_id, artifact_id=None):
        run = self.service.resource('runs', run_id, self.owner)
        if artifact_id:
            artifact = self.service.resource('artifacts', artifact_id, self.owner)
            if artifact['run_id'] != run_id:
                raise ServiceError(409, '报告版本与关联研究不一致')
            self.artifact_id, self.report_run_id = artifact_id, run_id
        self.thread_id, self.run_id = run['thread_id'], run['id']
        self.workspace_title.set_text(self.service.resource('threads',self.thread_id,self.owner)['title'])
        self.refresh_left()
        self.refresh_run()
        self.refresh_views(force=True)
        with self.client:
            self.remember()

    def render_knowledge_state(self):
        state = self.state
        if not state or not state.get('runtime'):
            return
        if state.get('parent'):
            with ui.row().classes('items-center gap-2'):
                ui.label('这是关联追问，本次行情与财报重新查询。').classes('muted')
                ui.button('查看前次研究', on_click=lambda: self.go_to_run(state['parent']['parent_run_id'], state['parent']['parent_artifact_id'])).props('flat size=sm')
        memories, snapshots = state.get('memory_usage', []), state.get('context_snapshots', [])
        with ui.expansion('上下文与使用的记忆', icon='psychology', value=bool(memories)).classes('w-full'):
            if not memories:
                ui.label('本次未引用长期记忆。').classes('muted')
            for m in memories:
                label = '已使用' if m['last_used_at'] else '已选中，等待模型读取'
                if m['status'] == 'withdrawn':
                    label = '已撤回；历史使用记录保留' if m['last_used_at'] else '已撤回，未使用'
                ui.label(KINDS[m['content']['kind']] + f" · v{m['memory_version']} · {label}").classes('eyebrow')
                ui.label(m['content']['content']).classes('text-sm leading-6')
                if m['content'].get('valid_as_of'):
                    ui.label('原历史时点 '+stamp(m['content']['valid_as_of'])+' · 有效期 '+stamp(m['content']['expires_at'])).classes('muted')
                if m['content']['kind'] in {'hypothesis','open_question'}:
                    ui.label('用户研究记录，尚未验证。').classes('muted')
            if snapshots:
                latest = snapshots[-1]
                ui.label(('已压缩上下文' if latest['compressed'] else '上下文在预算内') + f" · 估算 {latest['before_tokens']:,} → {latest['after_tokens']:,} tokens").classes('muted')
                ui.label('目标、约束、任务、审批、口径、证据与缺口均保留。').classes('muted')
            else:
                ui.label('模型规划开始后会记录实际上下文。').classes('muted')
            ui.button('查看上下文记录', on_click=self.context_dialog).props('flat size=sm')

    def context_dialog(self):
        snapshots = self.service.contexts.snapshots(self.owner, self.run_id)
        with self.client, ui.dialog() as dialog, ui.column().classes('surface dialog-card'):
            ui.label('上下文与压缩记录').classes('panel-title')
            ui.label('此处的 token 数为估算；供应商模型用量见运行详情。完整工具响应通过证据保存，结构化压缩不新增模型调用。').classes('muted')
            if not snapshots:
                ui.label('尚未构建模型上下文。').classes('muted')
            for s in reversed(snapshots):
                with ui.column().classes('editor-task'):
                    ui.label(('预算触发压缩' if s['compressed'] else '未触发压缩')+' · '+s['phase']).classes('font-semibold')
                    ui.label(f"估算 {s['before_tokens']:,} → {s['after_tokens']:,} tokens · {stamp(s['created_at'])}").classes('muted')
                    ui.label(f"原文 {s['summary']['entry_count']} 条事件 · 保留关键状态校验指纹").classes('muted')
                    ui.button('查看摘要与关键状态', on_click=lambda _, identifier=s['id']: self.context_snapshot_dialog(identifier)).props('outline size=sm')
            ui.button('关闭', on_click=dialog.close).props('flat')
        dialog.open()

    def context_snapshot_dialog(self, identifier):
        try:
            record = self.service.contexts.read(self.owner, identifier)
            self.json_dialog('上下文摘要与原文范围', {'summary': record['summary'], 'original_ranges': record['ranges'], 'protected_state': record['sent']['protected_state'],
                'sent_history': record['sent'].get('history_summary', record['sent'].get('history')), 'evidence_storage': '按证据 ID 读取原始响应或分页字段'})
        except ServiceError as e:
            ui.notify(e.message, type='warning')

    def render_artifact_actions(self, artifact, artifacts):
        ui.select({a['id']: f"版本 {a['version']} · {stamp(a['created_at'], True)}" for a in artifacts}, value=artifact['id'], label='报告版本', on_change=self.select_artifact).props('outlined dense').classes('w-full')
        with ui.row().classes('small-control gap-1'):
            ui.button('追问本次研究', icon='chat', on_click=self.followup_dialog).props('outline size=sm')
            ui.button('重新校验并保存版本', icon='verified', on_click=self.revise_report).props('flat size=sm')
        with ui.row().classes('small-control gap-1'):
            for format, label in (('markdown','Markdown'),('html','HTML'),('csv','结论 CSV'),('evidence','证据索引')):
                ui.button('导出 '+label, on_click=lambda _, f=format, identifier=artifact['id']: self.download_report(identifier, f)).props('flat size=sm')
            ui.link('打印 / PDF', f"/api/artifacts/{artifact['id']}/print", new_tab=True).classes('text-sm text-primary px-2 py-2')
        ui.button('提出偏好候选', icon='lightbulb', on_click=self.propose_preference).props('flat size=sm')

    def revise_report(self):
        try:
            latest = self.service.artifacts.latest(self.owner, self.run_id)
            artifact = self.service.artifacts.revise(self.owner, self.run_id, ReportRevision(expected_version=latest['version']))
            self.artifact_id = artifact['id']
            self.refresh_run()
            with self.client:
                self.remember()
                ui.notify(f"报告版本 {artifact['version']} 已保存")
        except ServiceError as e:
            with self.client:
                ui.notify(e.message, type='warning')

    def download_report(self, identifier, format):
        try:
            data, media, filename = export_report(self.service, self.owner, identifier, format)
            ui.download(data, filename=filename, media_type=media)
        except ServiceError as e:
            ui.notify(e.message, type='warning')

    def followup_dialog(self):
        with self.client, ui.dialog() as dialog, ui.column().classes('surface dialog-card'):
            ui.label('关联追问').classes('panel-title')
            ui.label('追问关联当前选中的报告版本，创建新的运行；采用目标栏的预算，按本次时点重新取数。').classes('muted')
            goal = ui.textarea('追问目标', placeholder='例如：进一步比较两家公司的基本每股收益，并列出披露日期。').props('outlined').classes('w-full')
            error = ui.label('').classes('text-negative text-sm')
            parent, artifact_id = self.run_id, self.artifact_id
            def submit():
                try:
                    run = self.service.artifacts.followup(self.owner, parent, FollowUpCreate(goal=goal.value or '', parent_artifact_id=artifact_id, budget=self.budget))
                    self.run_id, self.artifact_id = run['id'], None
                    self.goal.set_value('')
                    self.refresh_left()
                    self.refresh_run()
                    self.remember()
                    self.view_tabs.set_value('对话')
                    self.client.run_javascript('window.xbuddyChat.jump()')
                    dialog.close()
                    ui.notify('追问已提交为新的关联研究')
                except ServiceError as e:
                    error.set_text(e.message)
                except ValueError:
                    error.set_text('请填写 1–4000 字的追问目标。')
            with ui.row().classes('w-full justify-end'):
                ui.button('取消', on_click=dialog.close).props('flat')
                ui.button('开始追问研究', on_click=submit).classes('btn-primary')
        dialog.open()

    def propose_preference(self):
        try:
            items = self.service.memories.propose(self.owner, self.run_id)
            ui.notify('候选已生成，请确认后保存为长期偏好' if items else '本次研究没有可提取的价格口径偏好')
            self.memory_dialog()
        except ServiceError as e:
            ui.notify(e.message, type='warning')

    def memory_dialog(self):
        with self.client, ui.dialog() as dialog, ui.column().classes('surface dialog-card'):
            ui.label('长期记忆管理').classes('panel-title')
            ui.label('研究偏好可跨研究使用；事实保留来源、历史时点和有效期。推断候选需确认。删除或修正会停止旧版本的后续引用，已有研究保留当时的使用记录。').classes('muted')
            with ui.row().classes('w-full items-center'):
                search = ui.input('搜索记忆', placeholder='内容、标签或标的代码').props('outlined dense').classes('flex-1')
                ui.button('新增记忆', icon='add', on_click=lambda: self.memory_editor(on_saved=reload)).props('outline')
            box = ui.column().classes('w-full gap-3')
            def act(fn):
                try:
                    fn()
                    reload()
                    self.refresh_run()
                except ServiceError as e:
                    with self.client:
                        ui.notify(e.message, type='warning')
            def reload():
                box.clear()
                records = self.service.memories.list(self.owner, search.value or '')
                with box:
                    if not records:
                        ui.label('暂无符合条件的记忆。').classes('muted')
                    for m in records:
                        c = m['content']
                        with ui.column().classes('editor-task'):
                            ui.label(KINDS.get(c.get('kind'), '旧记录') + f" · v{m['version']} · "+MEMORY_STATUS.get(m['status'],m['status'])).classes('font-semibold')
                            ui.label(c['content']).classes('text-sm leading-6')
                            ui.label(c.get('write_basis','待复核') + ' · '+('本研究空间' if m['thread_id'] else '跨研究')).classes('muted')
                            if c.get('valid_as_of'):
                                ui.label('历史时点：'+stamp(c['valid_as_of'])).classes('muted')
                            ui.label('有效期：'+(stamp(c['expires_at']) if c.get('expires_at') else '长期')).classes('muted')
                            with ui.row().classes('small-control gap-1'):
                                if m['status'] == 'pending':
                                    for decision, label in (('approve','确认保存'),('reject','拒绝候选')):
                                        ui.button(label, on_click=lambda _, m=m, d=decision: act(lambda: self.service.memories.decide(self.owner, m['id'], MemoryDecision(decision=d, expected_version=m['version'], content_fingerprint=m['content_sha256'])))).props('outline size=sm')
                                ui.button('修正', on_click=lambda _, m=m: self.memory_editor(m, on_saved=reload)).props('flat size=sm')
                                ui.button('版本记录', on_click=lambda _, m=m: self.json_dialog('记忆版本记录', self.service.memories.versions(self.owner, m['id']))).props('flat size=sm')
                                if c.get('evidence_ids'):
                                    ui.button('查看事实依据', on_click=lambda _, e=c['evidence_ids'][0]: self.show_evidence(e)).props('flat size=sm')
                                ui.button('删除', on_click=lambda _, m=m: act(lambda: self.service.memories.delete(self.owner, m['id'], m['version']))).props('flat size=sm color=negative')
            search.on_value_change(lambda _: reload())
            reload()
            ui.button('关闭', on_click=dialog.close).props('flat')
        dialog.open()

    def memory_editor(self, record=None, *, source_artifact_id=None, claim_index=None, on_saved=None):
        c = record['content'] if record else {}
        source_artifact_id = source_artifact_id or c.get('source_artifact_id')
        claim_index = claim_index if claim_index is not None else c.get('claim_index')
        initial_kind = c.get('kind', 'research_fact' if source_artifact_id else 'preference')
        if initial_kind == 'research_fact':
            try:
                artifact = self.service.artifacts.verify(self.owner, source_artifact_id)
                content_value = claim_text(artifact['content']['claims'][claim_index])
            except ServiceError as e:
                ui.notify(e.message, type='warning')
                return
        else:
            content_value = c.get('content','')
        with self.client, ui.dialog() as dialog, ui.column().classes('surface dialog-card'):
            ui.label('修正记忆' if record else '保存长期记忆').classes('panel-title')
            kinds = {'research_fact':'历史研究事实'} if initial_kind == 'research_fact' else {k:v for k,v in KINDS.items() if k!='research_fact'}
            kind = ui.select(kinds, value=initial_kind, label='记忆类型').props('outlined').classes('w-full')
            content = ui.textarea('记忆内容', value=content_value, placeholder='例如：研究比较默认使用前复权口径').props('outlined').classes('w-full')
            if initial_kind == 'research_fact':
                content.disable()
                ui.label('内容绑定所选报告的已验证字段；有效期最多 30 天，原历史时点与单位保持原记录。').classes('muted')
            tags = ui.input('标签（逗号分隔）', value=','.join(c.get('tags', []))).props('outlined').classes('w-full')
            symbols = ui.input('关联标的（可选，完整代码）', value=','.join(c.get('symbols', []))).props('outlined').classes('w-full')
            scope = ui.select({'global':'跨研究', 'thread':'仅本研究空间'}, value='thread' if record and record['thread_id'] else 'global', label='使用范围').props('outlined').classes('w-full')
            expires_value = datetime.fromisoformat(c['expires_at']).astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M') if c.get('expires_at') else (datetime.now(SHANGHAI)+timedelta(days=1)).strftime('%Y-%m-%d %H:%M') if initial_kind=='research_fact' else ''
            expires = ui.input('有效期截止（上海时间，YYYY-MM-DD HH:MM）', value=expires_value, placeholder='偏好留空可长期；事实默认一天').props('outlined').classes('w-full')
            error = ui.label('').classes('text-negative text-sm')
            def save():
                try:
                    if scope.value == 'thread' and not self.thread_id:
                        raise ServiceError(422, '请先创建研究空间再使用该范围')
                    values = dict(kind=kind.value, content=content.value or '', tags=[x.strip() for x in (tags.value or '').split(',') if x.strip()],
                        symbols=[x.strip().upper() for x in (symbols.value or '').split(',') if x.strip()], thread_id=self.thread_id if scope.value=='thread' else None,
                        expires_at=datetime.strptime(expires.value, '%Y-%m-%d %H:%M').replace(tzinfo=SHANGHAI) if expires.value else None,
                        source_artifact_id=source_artifact_id, claim_index=claim_index)
                    if record:
                        self.service.memories.update(self.owner, record['id'], MemoryUpdate(**values, expected_version=record['version']))
                    else:
                        self.service.memories.create(self.owner, MemoryCreate(**values))
                    if on_saved:
                        on_saved()
                    dialog.close()
                    ui.notify('记忆已保存')
                except ServiceError as e:
                    error.set_text(e.message)
                except ValueError:
                    error.set_text('请核对内容、代码、标签和日期格式。')
            with ui.row().classes('w-full justify-end'):
                ui.button('取消', on_click=dialog.close).props('flat')
                ui.button('保存记忆', on_click=save).classes('btn-primary')
        dialog.open()
