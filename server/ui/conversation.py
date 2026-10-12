"""Incremental conversation presentation, with evidence tied to each saved answer."""
from __future__ import annotations

from nicegui import ui
from server.services import ServiceError
from server.storage.database import fingerprint
from server.ui.presentation import LABELS, narrative_text, stamp
from server.publication import PARAGRAPH_LABELS


class ConversationWorkbench:
    def init_conversation(self):
        self._message_thread=None
        self._message_ids=set()
        self._message_rows=[]
        self._chat_reports={}
        self._action_hash=None
        self._drafts={}
        self.active_view='对话'
        self._report_hash=self._process_hash=None

    def render_references(self, report, refs, artifact_id=None):
        with ui.row().classes('citation-row'):
            for ref in dict.fromkeys(refs):
                try:
                    index=int(ref[1:])
                    claim=report['claims'][index]
                    if ref!='F'+str(index) or index<0 or claim['kind']!='fact':continue
                except (ValueError,IndexError,KeyError):
                    continue
                ui.button(f'[{index+1}]',on_click=lambda _,c=claim,a=artifact_id:self.open_reference(c,a)).props(
                    f'flat dense size=sm aria-label="查看引用 [{index+1}]"').classes('citation-button')

    def open_reference(self, claim, artifact_id=None):
        try:
            if artifact_id:
                report=self.service.artifacts.verify(self.owner,artifact_id)['content']
                if claim not in report['claims']:raise ServiceError(409,'引用与报告版本不一致')
            self.show_claim(claim,artifact_id=artifact_id)
        except ServiceError as error:
            ui.notify(error.message,type='warning')

    def show_answer_report(self, run_id, artifact_id):
        self.go_to_run(run_id,artifact_id)
        self.view_tabs.set_value('研究报告')

    def render_message(self, message):
        user=message['role']=='user'
        with ui.element('article').classes('chat-message '+('chat-message--user' if user else 'chat-message--assistant')).props(
            f'data-message-id={message["id"]} aria-label="'+('你的问题' if user else 'XBuddy 回答')+'"') as node:
            if not user:
                with ui.element('div').classes('assistant-avatar'):
                    ui.icon('auto_awesome').props('aria-hidden=true')
            with ui.column().classes('message-bubble'):
                with ui.row().classes('message-meta'):
                    ui.label('你' if user else 'XBuddy').classes('message-author')
                    ui.label(stamp(message['created_at'],True)).classes('message-time')
                content=message['content']
                if user:
                    ui.label(content['text']).classes('user-text')
                else:
                    artifact_id=content.get('artifact_id')
                    report=None
                    if artifact_id and not content.get('publication_status')=='blocked':
                        if artifact_id not in self._chat_reports:
                            try:self._chat_reports[artifact_id]=self.service.artifacts.verify(self.owner,artifact_id)['content']
                            except ServiceError:self._chat_reports[artifact_id]=None
                        report=self._chat_reports[artifact_id]
                    if content.get('publication_status')=='blocked':
                        ui.label('回答未通过发布校验').classes('eyebrow')
                        ui.label(content['text']).classes('message-body')
                    elif report and content.get('answer_mode')=='error':
                        ui.label('本次执行未完成').classes('eyebrow')
                        ui.label(content['text']).classes('message-body')
                        def details(r=message['run_id']):
                            self.go_to_run(r, None)
                            self.view_tabs.set_value('研究过程')
                        ui.button('查看失败详情', on_click=details).props('flat size=sm')
                    elif report and content.get('answer_mode')=='research':
                        sections=report.get('narrative',[])
                        if sections:
                            for section in sections:
                                with ui.column().classes('message-section'):
                                    ui.label(section['title']).classes('message-section-title')
                                    for paragraph in section['paragraphs']:
                                        ui.label(PARAGRAPH_LABELS[paragraph['kind']]).classes('eyebrow')
                                        ui.markdown(narrative_text(paragraph,report),sanitize=True).classes('message-body')
                                        if paragraph.get('fact_refs'):
                                            self.render_references(report,paragraph['fact_refs'],artifact_id)
                        else:
                            ui.label('本次尚未生成通过校验的分析正文。').classes('message-body')
                            facts=[(i,c) for i,c in enumerate(report['claims']) if c['kind']=='fact'][:6]
                            if facts:
                                ui.label('已核验数据').classes('message-section-title')
                                ui.label('以下是工具取得的事实，不能替代完整分析。').classes('message-note')
                                for index,claim in facts:
                                    ui.label(claim['text']).classes('message-body')
                                    self.render_references(report,['F'+str(index)],artifact_id)
                        for failure in report.get('execution_failures',[]):
                            from server.ui.presentation import reason
                            ui.label(reason(failure['reason'])+'：'+failure['detail']).classes('message-note')
                        with ui.row().classes('answer-footer'):
                            ui.label(LABELS.get(report['status'],report['status'])).classes('answer-status')
                            ui.button('查看研究报告',icon='arrow_forward',on_click=lambda _,r=message['run_id'],a=artifact_id:self.show_answer_report(r,a)).props('flat size=sm')
                    elif artifact_id and not report:
                        ui.label('这份历史回答的报告需要重新校验，请打开报告查看具体原因。').classes('message-body')
                        ui.button('查看研究报告',on_click=lambda _,r=message['run_id'],a=artifact_id:self.show_answer_report(r,a)).props('flat size=sm')
                    else:
                        if content.get('answer_mode')=='knowledge':
                            ui.label(PARAGRAPH_LABELS['knowledge']).classes('eyebrow')
                        ui.markdown(content['text'],sanitize=True).classes('message-body')
                    if content.get('answer_mode')=='knowledge':
                        ui.label('一般知识 · 未查询本次金融数据').classes('message-note')
        return node

    def render_conversation(self):
        reset=self._message_thread!=self.thread_id
        if reset:
            self.conversation_box.clear()
            self._message_thread=self.thread_id
            self._message_ids=set()
            self._message_rows=[]
            self._chat_reports={}
            self._action_hash=None
            self.client.run_javascript('window.xbuddyChat?.reset()')
        page=self.service.dialogue.message_page(self.owner,self.thread_id) if self.thread_id else {'messages':[],'has_more':False}
        self.chat_empty.set_visibility(not page['messages'] and not self._message_rows)
        for message in page['messages']:
            if message['id'] not in self._message_ids:
                with self.conversation_box:self.render_message(message)
                self._message_ids.add(message['id'])
                self._message_rows.append(message)
        if reset or not self._message_rows:
            self.load_more.set_visibility(page['has_more'])
        self.render_chat_actions()

    def load_older_messages(self):
        try:
            if not self._message_rows:return
            page=self.service.dialogue.message_page(self.owner,self.thread_id,before=min(self._message_ids))
            older=[m for m in page['messages'] if m['id'] not in self._message_ids]
            for index,message in enumerate(older):
                with self.conversation_box:node=self.render_message(message)
                node.move(target_index=index)
                self._message_ids.add(message['id'])
            self._message_rows=older+self._message_rows
            self.load_more.set_visibility(page['has_more'])
        except ServiceError as error:
            ui.notify(error.message,type='warning')

    def render_chat_actions(self):
        state=self.state or {}
        tasks=state.get('tasks',[])
        processed=sum(t['status'] in {'completed','failed','blocked'} for t in tasks)
        pending=state.get('pending_input')
        approvals=[a for a in state.get('approvals',[]) if a['status']=='pending']
        signature=fingerprint({'run':self.run_id,'status':state.get('status'),'processed':processed,'total':len(tasks),
            'pending':pending,'approvals':approvals})
        self.start_button.set_text('发送答复' if pending else '发送')
        if signature==self._action_hash:return
        self._action_hash=signature
        self.chat_actions.clear()
        with self.chat_actions:
            status=state.get('status')
            if status=='awaiting_input' and pending:
                self.render_input(pending,include_question=False)
            for approval in approvals:self.render_approval(approval)
            if status in {'queued','running','paused','recoverable_failed'}:
                with ui.row().classes('chat-progress'):
                    if status in {'queued','running'}:ui.spinner(size='xs')
                    text=('正在理解你的问题…' if not tasks else '正在整理研究回答…' if processed==len(tasks)
                          else f'正在执行研究 · {processed}/{len(tasks)} 项已处理') if status=='running' else LABELS.get(status,status)
                    ui.label(text)
                    ui.button('查看研究过程',on_click=lambda:self.view_tabs.set_value('研究过程')).props('flat dense size=sm')

    def refresh_views(self, force=False):
        state=self.state or {}
        signature=fingerprint(state)
        if self.active_view=='研究过程' and (force or signature!=self._process_hash):
            self.render_runtime()
            self._process_hash=signature
        if self.active_view=='研究报告':
            key=fingerprint({'run':self.run_id,'artifacts':state.get('artifacts',[]),'selected':self.artifact_id,
                'status':state.get('status'),'evidence':state.get('evidence',[])})
            if force or key!=self._report_hash:
                self.render_report()
                self.render_evidence()
                self._report_hash=key
