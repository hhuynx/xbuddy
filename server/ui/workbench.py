from __future__ import annotations

from server.publication import PARAGRAPH_LABELS

import asyncio
import json
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request
from nicegui import ui

from server.events import latest_cursor, workspace_batches, workspace_snapshot
from server.models import ApprovalDecision, PlanUpdate, RunBudget, ThreadCreate
from server.security import session_owner
from server.services import ServiceError, view
from server.storage.database import encode, fingerprint, now
from server.ui.presentation import EVENTS, FIELDS, LABELS, SHANGHAI, claim_text, financial_rows, narrative_text, number, price_charts, raw_value, reason, stamp
from server.ui.knowledge import KnowledgeWorkbench
from server.ui.conversation import ConversationWorkbench

EXAMPLE = "比较贵州茅台600519.SH与五粮液000858.SZ最近60个交易日的前复权历史价格表现、波动率和最大回撤，并比较最近共同年报营业收入及归母净利润，列出来源、时点、单位和缺口。"
TERMINAL = {"completed", "partial", "blocked", "stopped", "failed"}


def badge(status):
    variant = "ok" if status in {"available", "completed", "passed", "complete"} else "warn" if status in {"paused", "awaiting_approval", "partial", "unverified", "stale", "missing", "queued"} else "bad" if status in {"failed", "blocked", "unauthorized", "conflict", "recoverable_failed"} else ""
    return ui.label(LABELS.get(status, status or "等待结果")).classes("status-badge " + variant)


def empty(icon, title, message):
    with ui.column().classes("empty-box"):
        ui.icon(icon).classes("text-3xl opacity-50")
        ui.label(title).classes("empty-title")
        ui.label(message).classes("muted")


class Workbench(ConversationWorkbench, KnowledgeWorkbench):
    def __init__(self, service, owner):
        self.service, self.owner = service, owner
        self.client = ui.context.client
        self.thread_id, self.run_id, self.state = None, None, None
        self.artifact_id, self.report_run_id = None, None
        self.cursor, self.feed_task, self.selecting = 0, None, False
        self.budget = RunBudget()
        self.init_conversation()
        self.build()
        threads = self.service.threads(owner)
        if threads:
            self.select_thread(threads[0]["id"], persist=False)
        else:
            self.refresh_left()
            self.refresh_run()
        self.client.on_connect(self.connected)
        self.client.on_disconnect(self.disconnected)

    def build(self):
        ui.colors(primary="#0f766e", secondary="#5469ad", negative="#a34f47", warning="#a97724")
        ui.add_head_html("<style>" + Path(__file__).with_name("workbench.css").read_text(encoding="utf-8") + "</style>")
        ui.add_head_html('<script>'+Path(__file__).with_name('workbench.js').read_text(encoding='utf-8')+'</script>')
        with ui.element("header").classes("app-header"):
            with ui.row().classes("items-center gap-3"):
                ui.button(icon='menu',on_click=self.toggle_sidebar).props('flat round aria-label="切换研究列表"').classes('sidebar-toggle')
                with ui.element("div").classes("brand-mark"):
                    ui.icon("auto_awesome").classes("text-xl")
                with ui.column().classes("gap-0"):
                    ui.label("XBuddy").classes("brand-name")
                    ui.label("个人投资研究工作台").classes("brand-sub")
            self.sync = ui.label("正在连接").classes("sync-badge")
        ui.element('button').classes('sidebar-scrim').props('aria-label="关闭研究列表"').on('click',self.toggle_sidebar)
        with ui.element("main").classes("workspace-grid"):
            self.left = ui.column().classes("research-sidebar")
            with self.left:
                with ui.row().classes("w-full items-center justify-between px-2"):
                    ui.label("研究空间").classes("panel-title")
                    ui.button(icon="add", on_click=self.new_thread_dialog).props("flat round size=sm aria-label=新建研究空间")
                ui.button("新建研究", icon="add", on_click=self.new_thread_dialog).classes("btn-primary w-full")
                self.threads_box = ui.column().classes("thread-list w-full gap-2")
                with ui.column().classes("sidebar-footer"):
                    from server.ui.documents import document_dialog
                    ui.button('研究资料',icon='description',on_click=lambda:document_dialog(self)).props('flat').classes('w-full')
                    ui.button("长期记忆", icon="psychology", on_click=self.memory_dialog).props("flat").classes("w-full")
                    ui.label("数据与模型").classes("eyebrow")
                    self.connection_box = ui.column().classes("w-full gap-2")
                    ui.button("查看工具能力", icon="extension", on_click=self.tools_dialog).props("flat size=sm").classes("w-full")
                    ui.label("供应商实际费用未知").classes("muted")
            with ui.column().classes('main-workspace'):
                with ui.row().classes('workspace-heading'):
                    self.workspace_title=ui.label('新的研究').classes('workspace-title')
                    self.run_select = ui.select({}, label="运行历史", on_change=self.history_changed).props("outlined dense").classes('run-history')
                with ui.row().classes('workspace-navigation'):
                    with ui.tabs(value='对话',on_change=self.change_view).classes('view-tabs') as self.view_tabs:
                        for name in ('对话','研究报告','研究过程'):ui.tab(name)
                    with ui.row().classes('small-control gap-1') as self.controls:
                        self.pause_button=ui.button('暂停',icon='pause',on_click=lambda:self.control('pause')).props('flat size=sm')
                        self.resume_button=ui.button('继续研究',icon='play_arrow',on_click=lambda:self.control('resume')).props('flat size=sm')
                        self.stop_button=ui.button('停止',icon='stop',on_click=lambda:self.control('stop')).props('flat size=sm color=negative')
                with ui.element('div').classes('view-deck'):
                    self.chat_page=ui.column().classes('view-panel chat-page is-active')
                    with self.chat_page:
                        with ui.element('div').classes('chat-scroll'):
                            with ui.column().classes('chat-content'):
                                self.load_more=ui.button('加载更早的对话',icon='expand_less',on_click=self.load_older_messages).props('flat size=sm').classes('load-history')
                                self.load_more.on('click',js_handler='() => window.xbuddyChat.beforePrepend()')
                                with ui.column().classes('chat-empty') as self.chat_empty:
                                    ui.icon('auto_awesome').classes('text-3xl text-primary')
                                    ui.label('从一个研究问题开始').classes('empty-title')
                                    ui.label('讨论研究方法，或给出股票与研究目标。').classes('muted')
                                    with ui.row().classes('example-prompts'):
                                        ui.button('怎么研究股票',on_click=lambda:self.goal.set_value('怎么研究股票')).props('outline')
                                        ui.button('历史表现与年报',on_click=lambda:self.goal.set_value(EXAMPLE)).props('outline')
                                self.conversation_box=ui.column().classes('conversation-box w-full')
                                self.chat_actions=ui.column().classes('chat-actions w-full')
                        with ui.button('回到最新',icon='arrow_downward').props('unelevated size=sm').classes('latest-button').on('click',js_handler='() => window.xbuddyChat.jump()'):
                            ui.label('有新消息').classes('new-message-note')
                        with ui.column().classes('chat-composer'):
                            self.goal=ui.textarea('研究目标 / 追问',placeholder='输入问题或继续追问…').props('outlined autogrow rows=2 maxlength=4000').classes('w-full goal-input')
                            self.goal.on('keydown',js_handler='e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) { e.preventDefault(); document.querySelector(".send-button")?.click(); } }')
                            with ui.row().classes('composer-toolbar'):
                                ui.button('预算设置',icon='tune',on_click=self.budget_dialog).props('flat size=sm')
                                self.budget_label=ui.label('').classes('budget-summary')
                                ui.label('Ctrl + Enter 发送').classes('composer-shortcut')
                                self.start_button=ui.button('发送',icon='arrow_upward',on_click=self.start_research).classes('btn-primary send-button')
                    self.report_page=ui.column().classes('view-panel report-page')
                    with self.report_page:
                        with ui.column().classes('report-content'):
                            self.report_box=ui.column().classes('surface report-document')
                            self.evidence_box=ui.column().classes('surface source-library')
                    self.process_page=ui.column().classes('view-panel process-page')
                    with self.process_page:
                        with ui.column().classes('process-content'):
                            with ui.row().classes('w-full justify-between items-center'):
                                ui.label('研究过程').classes('panel-title')
                                self.detail_button=ui.button('运行详情',icon='info',on_click=self.run_details).props('flat size=sm')
                            self.runtime_box=ui.column().classes('surface w-full gap-4')
                            with ui.expansion('手动数据查询',icon='manage_search').classes('surface'):
                                ui.label('按需查询并保存原始证据，作为独立运行记录。').classes('muted')
                                ui.button('打开查询面板',on_click=self.manual_dialog).props('outline')
        with ui.right_drawer(value=False).props("overlay bordered width=590 behavior=mobile").classes("evidence-drawer") as self.drawer:
            self.drawer_body = ui.column().classes("drawer-content")
        self.update_budget_label()

    def change_view(self, event):
        self.active_view=str(event.value)
        for name,pane in (('对话',self.chat_page),('研究报告',self.report_page),('研究过程',self.process_page)):
            pane.classes(add='is-active' if name==self.active_view else '',remove='' if name==self.active_view else 'is-active')
        self.refresh_views()
        self.client.run_javascript("window.dispatchEvent(new Event('resize'))")
        self.remember()

    def toggle_sidebar(self):
        self.client.run_javascript('window.xbuddyChat.toggleSidebar()')

    def update_budget_label(self):
        self.budget_label.set_text(f"工具 {self.budget.tool_attempts} 次 · 模型 {self.budget.model_calls} 次")

    def runs(self, thread_id):
        self.service.resource("threads", thread_id, self.owner)
        return [view(r) for r in self.service.db.all("SELECT * FROM runs WHERE thread_id=? AND session_id=? ORDER BY created_at DESC LIMIT 100", (thread_id, self.owner))]

    def refresh_left(self):
        self.threads_box.clear()
        with self.threads_box:
            threads = self.service.threads(self.owner)
            if not threads:
                empty("folder_open", "从一个问题开始", "新建研究空间，保留目标、运行和证据。")
            for thread in threads:
                with ui.row().classes("thread-item" + (" active" if thread["id"] == self.thread_id else "")):
                    ui.button(thread['title'], on_click=lambda _, t=thread['id']: self.select_thread(t)).props('flat no-caps align=left').classes('thread-open')
                    ui.button(icon='delete_outline', on_click=lambda _, t=thread['id']: self.delete_thread_dialog(t)).props('flat round dense size=sm aria-label="删除对话"').classes('thread-delete').tooltip('删除对话')
        self.connection_box.clear()
        tools = self.service.tools()
        with self.connection_box:
            for name, configured, usable in (
                ("扶摇", bool(self.service.settings.fuyao_api_key), any(t["provider"] == "fuyao" and t["status"] == "verified" for t in tools)),
                ("iFinD", bool(self.service.settings.ifind_mcp_url), any(t["provider"] == "ifind" and t["enabled"] for t in tools)),
                ("模型", self.service.harness.model.configured, self.service.harness.model.configured)):
                with ui.row().classes("w-full items-center justify-between gap-2"):
                    ui.label(name).classes("text-sm")
                    ui.label("已配置" if usable else "待验证" if configured else "未配置").classes("status-badge " + ("ok" if usable else "warn"))

    def select_thread(self, thread_id, persist=True):
        thread = self.service.resource("threads", thread_id, self.owner)
        if self.thread_id and self.thread_id!=thread_id:self._drafts[self.thread_id]=self.goal.value or ''
        self.thread_id = thread_id
        runs = self.runs(thread_id)
        self.workspace_title.set_text(thread['title'])
        self.goal.set_value(self._drafts.get(thread_id,thread['goal'] if not runs else ''))
        self.run_id = runs[0]["id"] if runs else None
        self.refresh_left()
        self.refresh_run()
        if persist:
            self.view_tabs.set_value('对话')
            self.client.run_javascript('document.body.classList.remove("sidebar-open")')
            self.remember()

    def delete_thread_dialog(self, thread_id):
        try:
            thread = self.service.resource('threads', thread_id, self.owner)
        except ServiceError as error:
            ui.notify(error.message, type='warning')
            return
        with self.client, ui.dialog() as dialog, ui.column().classes('surface dialog-card'):
            ui.label('删除对话').classes('panel-title')
            ui.label(f'确定删除「{thread["title"]}」？').classes('w-full')
            ui.label('对话将从列表移除，正在执行的研究会停止后续任务。').classes('muted')
            error = ui.label('').classes('text-negative text-sm')
            def remove():
                try:
                    self.service.delete_thread(self.owner, thread_id)
                    self._drafts.pop(thread_id, None)
                    self.reconcile_threads()
                    self.refresh_left()
                    self.remember()
                    dialog.close()
                    ui.notify('对话已删除', type='positive')
                except ServiceError as exc:
                    error.set_text(exc.message)
            with ui.row().classes('w-full justify-end'):
                ui.button('取消', on_click=dialog.close).props('flat')
                ui.button('确认删除', on_click=remove).props('color=negative')
        dialog.open()

    def reconcile_threads(self):
        threads = self.service.threads(self.owner)
        if self.thread_id and not any(t['id'] == self.thread_id for t in threads):
            removed = self.thread_id
            self._drafts.pop(removed, None)
            self.thread_id = self.run_id = self.artifact_id = self.report_run_id = None
            self.drawer.set_value(False)
            self.goal.set_value('')
            if threads:
                self.select_thread(threads[0]['id'])
            else:
                self.workspace_title.set_text('新的研究')
                self.view_tabs.set_value('对话')
                self.refresh_run()

    def history_changed(self, event):
        if self.selecting or not event.value or event.value == self.run_id:
            return
        run = self.service.resource("runs", event.value, self.owner)
        if run["thread_id"] == self.thread_id:
            self.run_id = run["id"]
            self.refresh_run()
            self.remember()

    def refresh_run(self):
        self.selecting = True
        runs = self.runs(self.thread_id) if self.thread_id else []
        self.run_select.set_options({r["id"]: ("自动研究" if r["mode"] == "agent" else "手动查询") + " · " + stamp(r["created_at"], True) + " · " + LABELS.get(r["status"], r["status"]) for r in runs}, value=self.run_id)
        self.selecting = False
        self.state = workspace_snapshot(self.service, self.owner, self.run_id)["run"] if self.run_id else None
        status=(self.state or {}).get('status')
        self.controls.set_visibility(bool((self.state or {}).get('runtime')))
        self.pause_button.set_visibility(status in {'queued','running'})
        self.resume_button.set_visibility(status in {'paused','recoverable_failed'})
        self.stop_button.set_visibility(bool(status) and status not in TERMINAL)
        self.detail_button.set_enabled(bool(self.state))
        self.run_select.set_visibility(bool(runs))
        self.render_conversation()
        self.refresh_views()

    async def connected(self):
        if self.client.is_deleted or not self.client.has_socket_connection:
            return
        with self.client:
            try:
                stored = await self.client.run_javascript("JSON.parse(sessionStorage.getItem('xbuddy.workbench') || '{}')", timeout=5)
                if self.client.is_deleted or not self.client.has_socket_connection:
                    return
                self.service.ensure_owner(self.owner)
                if isinstance(stored, dict):
                    if stored.get("thread"):
                        self.select_thread(stored["thread"], persist=False)
                    if stored.get("run"):
                        run = self.service.resource("runs", stored["run"], self.owner)
                        if run["thread_id"] == self.thread_id:
                            self.run_id = run["id"]
                            self.refresh_run()
                    if stored.get("artifact") and self.run_id:
                        artifact = self.service.resource("artifacts", stored["artifact"], self.owner)
                        if artifact["run_id"] == self.run_id:
                            self.artifact_id, self.report_run_id = artifact["id"], self.run_id
                    if stored.get('view') in {'对话','研究报告','研究过程'}:
                        self.view_tabs.set_value(stored['view'])
                    cursor = stored.get("cursor", self.cursor)
                    if type(cursor) is int and 0 <= cursor <= latest_cursor(self.service, self.owner):
                        self.cursor = cursor
            except ServiceError as error:
                if error.status == 401:
                    if not self.client.is_deleted and self.client.has_socket_connection:
                        self.sync.set_text('会话已过期，请刷新')
                        ui.notify(error.message, type='warning')
                    return
            except (TimeoutError, ValueError):
                pass
            if self.client.is_deleted or not self.client.has_socket_connection:
                return
            self.refresh_left()
            self.refresh_run()
            self.sync.set_text("实时同步")
            self.client.run_javascript('window.xbuddyChat.bind()')
            self.client.run_javascript("""if (!window.xbuddyConnectionBound && window.socket) {
                window.xbuddyConnectionBound = true;
                window.socket.on('disconnect', () => {
                    document.body.classList.add('connection-lost');
                    const badge = document.querySelector('.sync-badge');
                    if (badge) badge.textContent = '连接中断 · 正在恢复';
                });
                window.socket.on('connect', () => document.body.classList.remove('connection-lost'));
            } document.body.classList.remove('connection-lost');""")
            if self.feed_task:
                self.feed_task.cancel()
            self.feed_task = asyncio.create_task(self.listen(), name="xbuddy-view-" + self.client.id)
            self.remember()

    async def disconnected(self):
        if self.feed_task:
            self.feed_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.feed_task
            self.feed_task = None

    async def listen(self):
        try:
            async for batch in workspace_batches(self.service, self.owner, self.cursor):
                if self.client.is_deleted or not self.client.has_socket_connection:
                    return
                self.cursor = batch[-1]["id"]
                with self.client:
                    self.reconcile_threads()
                    self.refresh_left()
                    if self.thread_id and not self.run_id:
                        runs = self.runs(self.thread_id)
                        self.run_id = runs[0]["id"] if runs else None
                    if any(row["run_id"] == self.run_id for row in batch):
                        self.refresh_run()
                    else:
                        # Messages belong to the thread, even while an older Run
                        # is selected or another page starts the next research.
                        self.render_conversation()
                    self.sync.set_text("实时同步 · " + stamp(batch[-1]["created_at"], True))
                    self.remember()
        except asyncio.CancelledError:
            raise
        except ServiceError as error:
            if self.client.is_deleted or not self.client.has_socket_connection:
                return
            with self.client:
                self.sync.set_text("需要重新连接")
                ui.notify(error.message, type="warning")
        except Exception:
            if self.client.is_deleted or not self.client.has_socket_connection:
                return
            with self.client:
                self.sync.set_text("同步中断，请刷新恢复")

    def remember(self):
        self.client.run_javascript("sessionStorage.setItem('xbuddy.workbench', " + json.dumps(encode({"thread": self.thread_id, "run": self.run_id, "view":self.active_view,"artifact": self.artifact_id if self.report_run_id == self.run_id else None, "cursor": self.cursor})) + ")")

    def new_thread_dialog(self):
        with self.client, ui.dialog() as dialog, ui.column().classes("surface dialog-card"):
            ui.label("新建研究空间").classes("panel-title")
            title = ui.input("研究标题", placeholder="例如：白酒龙头比较").props("outlined").classes("w-full")
            target = ui.textarea("初始研究目标（可选）").props("outlined").classes("w-full")
            error = ui.label("").classes("text-negative text-sm")
            def save():
                try:
                    payload = ThreadCreate(title=title.value or "", goal=target.value or "")
                    thread = self.service.create_thread(self.owner, payload.title, payload.goal)
                    self.select_thread(thread["id"])
                    self.view_tabs.set_value('对话')
                    dialog.close()
                except ServiceError as exc:
                    error.set_text(exc.message)
                except Exception:
                    error.set_text("请填写 1–120 字的研究标题，目标最多 4000 字。")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("创建研究空间", on_click=save).classes("btn-primary")
        dialog.open()

    def budget_dialog(self):
        self.show_budget(self.budget, "本次研究预算", lambda b: (setattr(self, "budget", b), self.update_budget_label()))

    def show_budget(self, budget, title, on_save, maxima=None):
        values = budget.model_dump()
        limits = {**(maxima or {"tool_attempts": 100, "model_calls": 12, "model_tokens": 500000, "duration_seconds": 3600}), "context_tokens": (maxima or {}).get("context_tokens", 100000)}
        with self.client, ui.dialog() as dialog, ui.column().classes("surface dialog-card"):
            ui.label(title).classes("panel-title")
            ui.label("重试计入工具预算。等待确认、暂停和停机仍计入时限；供应商实际费用未知。").classes("muted")
            inputs = {}
            for key, name in (("tool_attempts", "工具尝试上限"), ("model_calls", "模型调用上限"), ("model_tokens", "模型 token 上限"), ("context_tokens", "上下文输入上限（估算 tokens）"), ("duration_seconds", "总时限（秒）")):
                inputs[key] = ui.number(name, value=values[key], min=10 if key == "duration_seconds" else 2000 if key == "context_tokens" else 0, max=limits[key], precision=0).props("outlined").classes("w-full")
                if maxima and key == "duration_seconds":
                    inputs[key].disable()
            error = ui.label("").classes("text-negative text-sm")
            def save():
                try:
                    on_save(RunBudget(**{k: int(v.value) for k, v in inputs.items()}))
                    dialog.close()
                except ServiceError as exc:
                    error.set_text(exc.message)
                except Exception:
                    error.set_text("预算需为有效整数，并在允许范围内。")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("保存预算", on_click=save).classes("btn-primary")
        dialog.open()

    def start_research(self):
        try:
            goal = (self.goal.value or "").strip()
            if not 1 <= len(goal) <= 4000:
                raise ServiceError(422, "请填写 1–4000 字的研究目标")
            if not self.service.harness.model.configured:
                raise ServiceError(503, "请先在本机 .env 配置模型服务")
            if not self.thread_id:
                self.thread_id = self.service.create_thread(self.owner, goal[:32], goal)["id"]
            pending = self.service.dialogue.pending(self.owner,self.run_id) if self.run_id else None
            if pending:
                self.service.dialogue.respond(self.owner,self.run_id,pending['id'],goal)
            else:
                self.run_id = self.service.harness.submit(self.owner, self.thread_id, goal, self.budget)["id"]
            self.goal.set_value('')
            self._drafts[self.thread_id]=''
            self.refresh_left()
            self.refresh_run()
            self.view_tabs.set_value('对话')
            self.client.run_javascript('window.xbuddyChat.jump()')
            self.remember()
            ui.notify("已提交，助手将继续处理")
        except ServiceError as exc:
            ui.notify(exc.message, type="warning")

    def input_click(self, pending, answer):
        try:
            self.service.dialogue.respond(self.owner,self.run_id,pending['id'],answer)
            self.refresh_run()
            self.client.run_javascript('window.xbuddyChat.jump()')
        except ServiceError as exc:
            ui.notify(exc.message,type='warning')

    def render_input(self, pending, include_question=True):
        with ui.column().classes('w-full warning-card'):
            ui.label('需要补充信息').classes('font-semibold')
            if include_question:ui.label(pending['question']['text']).classes('text-sm leading-6')
            candidates = {c['thscode']:c.get('name','') for c in pending['question'].get('candidates',[])}
            for option in pending['question'].get('options',[]):
                ui.button((candidates.get(option,'')+' '+option).strip(),on_click=lambda _,a=option,p=pending:self.input_click(p,a)).props('outline').classes('w-full')
            if pending['question'].get('kind') != 'entity':
                answer = ui.textarea('补充信息').props('outlined autogrow').classes('w-full')
                ui.button('提交补充并继续',on_click=lambda p=pending,a=answer:self.input_click(p,a.value or '')).classes('btn-primary')

    def control(self, action):
        try:
            self.service.harness.control(self.owner, self.run_id, action)
            self.refresh_left()
            self.refresh_run()
        except ServiceError as exc:
            ui.notify(exc.message, type="warning")

    def decide(self, approval, decision, budget=None):
        c = approval["content"]
        self.service.harness.decide(self.owner, approval["id"], ApprovalDecision(decision=decision,
            plan_version=c["plan_version"], parameters_fingerprint=c["parameters_fingerprint"], budget=budget))
        self.refresh_left()
        self.refresh_run()

    def decision_click(self, approval, decision):
        try:
            self.decide(approval, decision)
        except ServiceError as exc:
            ui.notify(exc.message, type="warning")

    def render_runtime(self):
        state = self.state or {}
        self.controls.set_visibility(bool(state.get("runtime")))
        self.pause_button.set_visibility(state.get("status") in {"queued", "running"})
        self.resume_button.set_visibility(state.get("status") in {"paused", "recoverable_failed"})
        self.stop_button.set_visibility(state.get("status") not in TERMINAL)
        self.runtime_box.clear()
        with self.runtime_box:
            state = self.state
            if not state:
                empty("route", "研究计划将显示在这里", "提交目标后，查看任务、预算确认和真实执行结果。")
                return
            with ui.row().classes("w-full justify-between items-center"):
                ui.label("本次运行").classes("panel-title")
                badge(state["status"])
            ui.label(state["goal"]).classes("text-sm leading-6")
            scope = state.get('agent',{}).get('entry',{}).get('resolved_goal')
            if scope and scope != state['goal']:
                ui.label('本次研究范围：'+scope).classes('text-sm leading-6')
            tasks = state["tasks"]
            processed = sum(t["status"] in {"completed", "failed", "blocked"} for t in tasks)
            completed = sum(t["status"] == "completed" for t in tasks)
            if tasks:
                ui.label(f"已处理 {processed}/{len(tasks)} 项 · 成功 {completed} 项").classes("muted")
                ui.linear_progress(processed/len(tasks), show_value=False, size="6px").classes("w-full")
            runtime, config = state.get("runtime"), state["config"]
            if runtime:
                with ui.row().classes("w-full gap-2"):
                    for name, used, total in (("工具尝试", state["attempts_used"], config["budget"]["tool_attempts"]), ("模型调用", runtime["model_calls"], config["budget"]["model_calls"])):
                        with ui.column().classes("budget-pill gap-1"):
                            ui.label(name).classes("budget-label")
                            ui.label(f"{used} / {total}").classes("font-semibold")
                if state["status"] == "queued":
                    ui.label("已进入后台队列，等待调度。").classes("muted")
                elif state["status"] == "running" and not tasks:
                    with ui.row().classes("items-center"):
                        ui.spinner(size="sm")
                        ui.label("助手正在理解问题或组织回答…").classes("muted")
                elif state['status'] == 'awaiting_input' and state.get('pending_input'):
                    ui.button('回到对话补充信息',on_click=lambda:self.view_tabs.set_value('对话')).props('outline')
                elif state['status'] == 'running' and tasks and processed == len(tasks):
                    with ui.row().classes('items-center'):
                        ui.spinner(size='sm')
                        ui.label('正在核对研究目标与证据，组织综合回答…').classes('muted')
                elif state["status"] == "paused":
                    ui.label("已暂停后续派发。当前请求结束后可修改未执行任务。").classes("muted")
                elif state["status"] == "recoverable_failed":
                    ui.label("已保存检查点，点击继续研究恢复；已完成结果会复用。").classes("warning-card text-sm")
                elif state["status"] == "stopped":
                    ui.label("研究已停止。已发出的请求结果保留，不再派发新调用。").classes("muted")
                for approval in state["approvals"]:
                    if approval["status"] == "pending":
                        ui.button('回到对话确认预算',on_click=lambda:self.view_tabs.set_value('对话')).props('outline')
                self.render_knowledge_state()
                with ui.row().classes("w-full justify-between items-center"):
                    ui.label("研究计划").classes("panel-title")
                    ui.button("修改计划", icon="edit", on_click=self.edit_plan_dialog).props("flat size=sm").set_enabled(bool(tasks) and state["status"] in {"paused", "awaiting_approval"})
                ui.label(f"计划版本 {state['plan_version']} · 已执行任务保留原始记录").classes("muted")
            calls = {c["task_id"]: c for c in state["tool_calls"]}
            tools = {t["name"]: t for t in self.service.tools()}
            for task in tasks:
                with ui.column().classes("task-row" + (" done" if task["status"] == "completed" else "")):
                    with ui.row().classes("w-full items-start justify-between gap-2"):
                        ui.label(task["title"]).classes("task-title flex-1")
                        badge(task["status"])
                    ui.label(tools.get(task["tool"], {}).get("description", task["tool"])).classes("task-meta")
                    if task["tool"] == "calculate_price_comparison":
                        observations = task["arguments"].get("trading_days", 60)
                        ui.label(f"当前口径：{observations} 个收盘观察值，对应 {observations-1} 个日收益").classes("task-meta")
                    if task["depends_on"]:
                        titles = {t["id"]: t["title"] for t in tasks}
                        ui.label("前置：" + "、".join(titles.get(d, "历史任务") for d in task["depends_on"])).classes("task-meta")
                    if task["id"] in calls:
                        self.render_call(calls[task["id"]])
            with ui.expansion('调用追踪',icon='timeline').classes('w-full'):
                from server.tracing import trace_view
                trace = trace_view(self.service,self.owner,state['id'])
                for span in trace['spans']:
                    label = {'research':'研究运行','model':'模型调用','tool':'工具调用'}.get(span['name'],span['name'])
                    label += ' · '+span['attributes'].get('tool',span['attributes'].get('phase',''))
                    ui.label(f"{label} · {LABELS.get(span['status'],span['status'])} · {span['elapsed_ms']/1000:.2f} 秒").classes('muted')
                for call in trace['model_usage']:
                    ui.label(f"{call['phase']} · token {(call.get('usage') or {}).get('total_tokens','未知')} · 实际费用未知").classes('muted')
            with ui.expansion("运行事件", icon="history").classes("w-full"):
                for event in reversed(self.service.events(self.owner, state["id"], limit=500)[-40:]):
                    with ui.row().classes("event-row"):
                        ui.label(stamp(event["created_at"], True)).classes("event-time")
                        ui.label(EVENTS.get(event["event_type"], "运行状态更新")).classes("flex-1")
                        ui.label("#" + str(event["id"])).classes("thread-meta")

    def render_call(self, call):
        summary = call.get("result") or {}
        with ui.row().classes("items-center gap-2"):
            if call.get("quality"):
                badge(call["quality"])
            ui.label(f"{call['attempts']} 次尝试 · {(call.get('elapsed_ms') or 0)/1000:.2f} 秒").classes("task-meta")
        if summary.get("record_count") is not None:
            ui.label(f"已保存 {summary['record_count']} 条原始记录").classes("task-meta")
        for issue in summary.get("issues", []):
            ui.label(reason(issue)).classes("muted")
        with ui.row().classes("small-control gap-1"):
            for eid in summary.get("evidence_ids", []):
                ui.button("查看证据", on_click=lambda _, eid=eid: self.show_evidence(eid)).props("flat size=sm")
            for mid in summary.get("derived_metric_ids", []):
                ui.button("计算依据", on_click=lambda _, mid=mid: self.show_metric(mid)).props("flat size=sm")
            ui.button("调用详情", on_click=lambda _, c=call: self.call_details(c)).props("flat size=sm")

    def render_approval(self, approval):
        proposed = approval["content"]["parameters"]["proposed_budget"]
        with ui.column().classes("approval-card"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("fact_check").classes("text-amber-700")
                ui.label("需要确认：扩大运行预算").classes("font-semibold")
            ui.label("后续操作已暂停。批准后按本次申请继续；拒绝会保留已获取结果并标记研究缺口。").classes("text-sm leading-6")
            ui.label(f"申请工具 {proposed['tool_attempts']} 次 · 模型 {proposed['model_calls']} 次 · token {proposed['model_tokens']:,}").classes("muted")
            ui.label(f"上下文输入上限 {proposed.get('context_tokens', 12000):,} tokens（估算）").classes("muted")
            with ui.row().classes("gap-2"):
                ui.button("批准", on_click=lambda: self.decision_click(approval, "approve")).classes("btn-primary")
                ui.button("拒绝", on_click=lambda: self.decision_click(approval, "reject")).props("outline")
                ui.button("修改预算", on_click=lambda: self.show_budget(RunBudget(**proposed), "修改本次审批预算", lambda b: self.decide(approval, "modify", b), proposed)).props("flat")

    def render_report(self):
        self.report_box.clear()
        with self.report_box:
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("研究报告").classes("panel-title")
                ui.label("可追溯 · 可复核").classes("eyebrow")
            state = self.state
            artifacts = [a for a in (state or {}).get("artifacts", []) if a["kind"] == "research_report"]
            if not artifacts:
                empty("description", "等待研究成果", "完成取数和校验后，报告、价格图表和财务比较将在这里呈现。")
                if state and state["status"] == "stopped":
                    ui.label("本次已停止，可在下方查看保留的来源证据。").classes("muted")
                return
            artifact = self.selected_artifact(artifacts)
            with ui.row().classes('report-toolbar w-full'):
                self.render_artifact_actions(artifact, artifacts)
            try:
                report = self.service.artifacts.verify(self.owner, artifact["id"])["content"]
            except ServiceError as exc:
                ui.label(exc.message + "，请重新校验并保存版本。").classes("error-card text-sm")
                return
            with ui.row().classes("items-center gap-2"):
                badge(report["status"])
                if report.get('answer_mode') != 'error':
                    badge(report["validation_status"])
                ui.label(stamp(report["created_at"])).classes("muted")
            if report["validation_status"] == "failed":
                with ui.column().classes("error-card"):
                    ui.label("核心结论未通过校验").classes("font-semibold")
                    ui.label("来源、计算或回答内容未通过校验，已阻止发布结论。请检查运行详情或重新研究。").classes("text-sm")
                return
            if report.get('execution_failures'):
                with ui.column().classes('error-card'):
                    ui.label('执行问题').classes('font-semibold')
                    for item in report['execution_failures']:
                        ui.label(reason(item['reason'])+'：'+item['detail']).classes('text-sm')
            if report["gaps"]:
                with ui.column().classes("warning-card"):
                    ui.label(('研究未完成' if report['status']=='failed' else '部分结果') + f" · {len(report['gaps'])} 项缺口").classes("font-semibold")
                    for gap in report["gaps"][:8]:
                        ui.label(reason(gap["reason"]) + ("：" + gap["detail"] if gap.get("detail") else "")).classes("text-xs leading-5")
            if report.get('answer_mode') == 'knowledge':
                ui.label('一般知识解释 · 未进行当前金融数据核验').classes('muted')
            if report.get('execution_issues'):
                with ui.expansion('调用与数据质量记录',icon='info').classes('w-full'):
                    for item in report['execution_issues']:
                        ui.label(reason(item['reason'])+('：'+item['detail'] if item.get('detail') else '')).classes('muted')
            for section in report.get('narrative',[]):
                with ui.column().classes('narrative-section'):
                    ui.label(section['title']).classes('panel-title')
                    for paragraph in section['paragraphs']:
                        if paragraph['kind']=='fact':continue
                        with ui.column().classes('analysis-paragraph '+paragraph['kind']):
                            ui.label(PARAGRAPH_LABELS[paragraph['kind']]).classes('eyebrow')
                            ui.markdown(narrative_text(paragraph,report),sanitize=True).classes('message-body')
                            self.render_references(report,paragraph.get('fact_refs',[]),artifact['id'])
                    data=[p for p in section['paragraphs'] if p['kind']=='fact']
                    if data:
                        with ui.expansion('相关核验数据',icon='fact_check').classes('w-full'):
                            for paragraph in data:
                                ui.label(narrative_text(paragraph,report)).classes('claim-title whitespace-pre-wrap')
                                self.render_references(report,paragraph.get('fact_refs',[]),artifact['id'])
            metric_claims = [c for c in report["claims"] if c["kind"] == "fact" and c.get("field") in {"historical_price_change_pct", "annualized_volatility_pct", "maximum_drawdown_pct"}]
            if metric_claims:
                with ui.element("div").classes("metrics-grid"):
                    for claim in metric_claims:
                        with ui.column().classes("metric-tile") as tile:
                            tile.props("role=button tabindex=0")
                            tile.on("click", lambda _, c=claim: self.show_claim(c))
                            tile.on("keydown.enter", lambda _, c=claim: self.show_claim(c))
                            ui.label(claim["symbol"]).classes("metric-symbol")
                            ui.label(number(claim["value"], claim["unit"])).classes("metric-value")
                            ui.label(FIELDS[claim["field"]]).classes("muted")
            charts, chart_issues = price_charts(self.service, self.owner, state["id"], state["evidence"])
            for chart in charts:
                ui.label("历史价格走势").classes("font-semibold")
                ui.label({"forward": "前复权", "backward": "后复权", "none": "未复权"}[chart["adjust"]] + " · 日收盘价 · 元/股 · 来源：扶摇").classes("muted")
                def point(event, chart=chart):
                    try:
                        ref = chart["point_refs"][event.series_index][event.data_index]
                        if ref:
                            self.show_evidence(ref["evidence_id"], ref["raw_path"])
                    except (IndexError, TypeError):
                        pass
                ui.echart(chart["options"], on_point_click=point).classes("w-full h-64")
                with ui.row().classes("small-control"):
                    for source in chart["sources"]:
                        ui.button("图表来源 " + source["symbol"], on_click=lambda _, e=source["evidence_id"]: self.show_evidence(e)).props("flat size=sm")
                ui.label("缺失观测保留为空；不补齐、不混合复权口径。可缩放查看区间。").classes("muted")
            for issue in chart_issues:
                ui.label("部分价格证据未用于图表：" + reason(issue["reason"])).classes("muted")
            rows = financial_rows(report)
            if rows:
                ui.label("已披露财务数据").classes("font-semibold")
                ui.label("报告期见表中日期；金额按需以亿元显示，原始单位与披露日期可点击依据查看。").classes("muted")
                columns = [{"name": "symbol", "label": "标的", "field": "symbol", "align": "left"}, {"name": "period", "label": "报告期", "field": "period", "align": "left"},
                    *[{"name": key, "label": FIELDS[key], "field": key, "align": "right"} for key in ("operating_income", "parent_holder_net_profit", "net_profit", "basic_eps")], {"name": "proof", "label": "依据", "field": "id"}]
                table = ui.table(columns=columns, rows=rows, row_key="id", pagination=5).props("flat dense wrap-cells").classes("financial-table")
                table.add_slot("body-cell-proof", '<q-td :props="props"><q-btn flat dense color="primary" label="查看依据" @click="$parent.$emit(\'proof\', props.row.id)" /></q-td>')
                table.on("proof", lambda e: self.show_claim(report["claims"][next(r for r in rows if r["id"] == str(e.args))["claim_indices"][0]]))
            for note in report.get("knowledge_context", {}).get("user_notes", []):
                ui.label("不确定 · 用户研究备注：" + note["text"]).classes("warning-card text-sm")
            facts=[(i,c) for i,c in enumerate(report['claims']) if c['kind']=='fact']
            with ui.expansion(f'全部核验事实（{len(facts)} 项）',icon='fact_check').classes('w-full'):
                for index,claim in facts:
                    with ui.column().classes('claim-card') as card:
                        card.props('role=button tabindex=0')
                        card.on('click',lambda _,c=claim:self.show_claim(c))
                        card.on('keydown.enter',lambda _,c=claim:self.show_claim(c))
                        ui.label(f'[{index+1}] '+claim_text(claim)).classes('claim-title')
                        ui.label('数据时点 '+stamp(claim.get('as_of'))).classes('claim-source')
            if report.get("limitations"):
                with ui.expansion("口径与研究边界", icon="info").classes("w-full"):
                    for text in report["limitations"]:
                        ui.label(text).classes("muted")
            ui.button("查看报告 JSON", icon="data_object", on_click=lambda: self.json_dialog("报告 JSON", report)).props("flat size=sm")

    def render_evidence(self):
        self.evidence_box.clear()
        evidence = (self.state or {}).get("evidence", [])
        with self.evidence_box, ui.expansion(f'来源与原始资料（{len(evidence)} 份）',icon='source').classes('w-full'):
            if not evidence:
                ui.label("尚未保存来源证据。工具返回后会显示质量、数据时点和原始记录。").classes("muted")
            for record in evidence:
                c = record["content"]
                with ui.row().classes("w-full items-center justify-between gap-2"):
                    with ui.column().classes("gap-1 flex-1 min-w-0"):
                        ui.label((c["parameters"].get("thscode") or "研究资料") + " · " + {'fuyao':'扶摇','ifind':'iFinD','documents':'导入原文'}.get(c['provider'],c['provider'])).classes("text-sm font-medium")
                        ui.label(stamp(c["as_of"]) + f" · {len(c['records'])} 条记录").classes("muted")
                    badge(c["quality"])
                    ui.button(icon="chevron_right", on_click=lambda _, e=record["id"]: self.show_evidence(e)).props("flat round size=sm aria-label=打开来源证据")

    def drawer_header(self, title):
        self.drawer_body.clear()
        with self.drawer_body:
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(title).classes("panel-title")
                ui.button(icon="close", on_click=self.drawer.hide).props("flat round aria-label=关闭证据抽屉")
        self.drawer.show()

    def show_evidence(self, evidence_id, path=None, claim=None):
        record = self.service.resource("evidence", evidence_id, self.owner)
        raw = self.service.resource("raw_records", record["raw_id"], self.owner)
        c = record["content"]
        self.drawer_header("证据与原始依据")
        with self.drawer_body:
            if claim:
                ui.label('支持的核验事实').classes('eyebrow')
                ui.label(claim_text(claim)).classes('font-semibold leading-6')
            with ui.row().classes("items-center gap-2"):
                ui.label({'fuyao':'扶摇','ifind':'iFinD','documents':'导入原文'}.get(c['provider'],c['provider'])).classes("font-semibold")
                badge(c["quality"])
            if c['provider']=='documents':
                for item in c['records']:
                    ui.link(item['title'],item['source_url'],new_tab=True)
                    ui.label(f"发布时间 {stamp(item['published_at'])} · 第 {item['page']} 页 · 字符 {item['offset_start']}–{item['offset_end']}").classes('muted')
                    ui.label(item['text']).classes('whitespace-pre-wrap')
                ui.label('已核验摘录与导入正文一致；原文陈述及来源真实性尚需外部核验。').classes('muted')
            if c['provider'] != 'documents':
                ui.label("数据时点：" + stamp(c["as_of"]) + "（上海时间）").classes("text-sm")
            if claim and claim.get('period_end_ms'):
                ui.label("所选报告期：" + stamp(claim['period_end_ms'])[:10]).classes("text-sm")
                ui.label("所选披露日期：" + stamp(claim['report_date_ms']) + "（供应商字段）").classes("text-sm")
                ui.label("会计年度：" + str(claim['fiscal_year']) + " · " + claim['currency'] + " · " + claim['basis']).classes("muted")
            ui.label("获取时间：" + stamp(c["fetched_at"])).classes("muted")
            ui.label("记录数量：" + str(len(c["records"]))).classes("muted")
            if path:
                with ui.column().classes("raw-field"):
                    ui.label(FIELDS.get(path.rsplit('.',1)[-1],'所选字段')).classes('font-semibold')
                    ui.label("原始值：" + encode(raw_value(raw["body"], path))).classes("text-lg font-medium")
                    ui.label("原始单位：" + c["units"].get(path.rsplit(".", 1)[-1], "见字段与口径")).classes("muted")
            with ui.expansion('单位与统计口径',icon='info').classes('w-full'):
                for key,value in c['units'].items():ui.label(FIELDS.get(key,key)+'：'+value).classes('muted')
                ui.code(json.dumps(c['method'],ensure_ascii=False,indent=2),language='json').classes('w-full')
            if c["issues"]:
                with ui.column().classes("warning-card"):
                    ui.label("资料质量提示").classes("font-semibold")
                    for issue in c["issues"]:
                        ui.label(reason(issue)).classes("text-sm")
            with ui.expansion('原始字段与响应',icon='data_object').classes('w-full'):
                if path:ui.label(path).classes('raw-path')
                ui.code(json.dumps(raw['body'],ensure_ascii=False,indent=2),language='json').classes('w-full max-h-96 overflow-auto')
            with ui.expansion("字段映射与证据校验信息", icon="verified").classes("w-full"):
                ui.code(json.dumps({"evidence_id": record["id"], "request_id": c["request_id"], "sha256": record["content_sha256"], "raw_sha256": raw["content_sha256"], "field_map": c["field_map"]}, ensure_ascii=False, indent=2), language="json").classes("w-full")

    def show_metric(self, metric_id, claim=None):
        metric = self.service.resource("derived_metrics", metric_id, self.owner)
        self.drawer_header("计算依据与原始输入")
        with self.drawer_body:
            if claim:
                ui.label(claim_text(claim)).classes("font-semibold leading-6")
                ui.label("未舍入结果：" + str(claim.get("value")) + " " + claim.get("unit", "")).classes("muted")
            badge(metric["content"]["quality"])
            with ui.expansion('计算公式与口径',icon='functions').classes('w-full'):
                ui.code(json.dumps(metric['content']['method'],ensure_ascii=False,indent=2),language='json').classes('w-full')
            ui.label("原始输入证据").classes("font-semibold")
            for ref in metric["input_refs"]:
                c = self.service.resource("evidence", ref["evidence_id"], self.owner)["content"]
                ui.label((c["parameters"].get("thscode") or "交易日历") + " · " + stamp(c["as_of"])).classes("text-sm")
                ui.button("核对原始输入字段", on_click=lambda _, ref=ref: self.show_evidence(ref["evidence_id"], ref["field_paths"][0] if ref["field_paths"] else None)).props("outline size=sm")
            with ui.expansion("精确输入字段索引", icon="list").classes("w-full"):
                ui.code(json.dumps(metric["input_refs"], ensure_ascii=False, indent=2), language="json").classes("w-full")
            with ui.expansion("完整计算结果", icon="calculate").classes("w-full"):
                ui.code(json.dumps(metric["content"], ensure_ascii=False, indent=2), language="json").classes("w-full")

    def show_claim(self, claim, *, artifact_id=None):
        if claim.get("derived_metric_ids"):
            self.show_metric(claim["derived_metric_ids"][0], claim)
        elif claim.get("evidence_ids"):
            ref = claim.get("reference") or {}
            self.show_evidence(ref.get("evidence_id", claim["evidence_ids"][0]), ref.get("raw_path"),claim)
        else:
            self.drawer_header("结论边界与未解决问题")
            with self.drawer_body:
                ui.label(claim["text"]).classes("text-sm leading-6")
                for value in claim.get("limitations", []):
                    ui.label(reason(value)).classes("muted")
        source_artifact=artifact_id or self.artifact_id
        if claim.get("kind") == "fact" and source_artifact:
            artifact = self.service.artifacts.verify(self.owner, source_artifact)
            index = next((i for i, item in enumerate(artifact["content"]["claims"]) if item == claim), None)
            if index is not None:
                with self.drawer_body:
                    ui.button("保存为历史事实", icon="bookmark_add", on_click=lambda: self.memory_editor(source_artifact_id=artifact["id"], claim_index=index)).props("outline")

    def json_dialog(self, title, data):
        with self.client, ui.dialog() as dialog, ui.column().classes("surface dialog-card"):
            ui.label(title).classes("panel-title")
            ui.code(json.dumps(data, ensure_ascii=False, indent=2), language="json").classes("w-full max-h-96 overflow-auto")
            ui.button("关闭", on_click=dialog.close).props("flat")
        dialog.open()

    def call_details(self, call):
        attempts = self.service.db.all("SELECT attempt,started_at,finished_at,elapsed_ms,http_status,business_code,error,cost_status FROM tool_attempts WHERE call_id=? ORDER BY attempt", (call["id"],))
        self.json_dialog("调用参数与尝试记录", {"tool": call["tool"], "parameters": call["parameters"], "result": call["result"], "attempts": attempts})

    def run_details(self):
        state, runtime = self.state, self.state.get("runtime") or {}
        with self.client, ui.dialog() as dialog, ui.column().classes("surface dialog-card"):
            ui.label("运行详情").classes("panel-title")
            badge(state["status"])
            ui.label("开始：" + stamp(runtime.get("started_at"))).classes("text-sm")
            if runtime.get("deadline"):
                ui.label("截止：" + stamp(runtime["deadline"]*1000)).classes("text-sm")
            ui.label("最近心跳：" + stamp(runtime.get("heartbeat_at"))).classes("text-sm")
            ui.label(f"累计执行耗时 {runtime.get('elapsed_ms', 0)/1000:.2f} 秒 · 工具尝试 {state['attempts_used']} 次").classes("text-sm")
            ui.label(f"模型 token 已记账 {runtime.get('model_tokens', 0):,} · 费用未知").classes("text-sm")
            for call in state.get("model_usage", []):
                usage = call.get("usage") or {}
                ui.label(call["phase"] + " · " + ("供应商报告用量" if usage.get("token_status") == "reported" else "用量估算/未确认") + f" · {call.get('elapsed_ms') or 0} ms").classes("muted")
            ui.label("暂停、等待确认和停机均计入总时限。停止后已发出的请求可能完成，结果保留。").classes("muted")
            ui.button("关闭", on_click=dialog.close).props("flat")
        dialog.open()

    def tools_dialog(self):
        with self.client, ui.dialog() as dialog, ui.column().classes("surface dialog-card"):
            ui.label("工具能力与连接状态").classes("panel-title")
            ui.label("仅使用已配置且获许可的只读工具。状态以当前账号实际调用为准。").classes("muted")
            for tool in self.service.tools():
                with ui.column().classes("editor-task"):
                    ui.label(tool["description"]).classes("font-semibold")
                    ui.label(tool["provider"] + " · " + ("可用" if tool["enabled"] else "未启用") + " · " + tool["status"]).classes("muted")
                    ui.label(f"超时 {tool['timeout_seconds']} 秒 · 最多重试 {tool['max_retries']} 次 · " + ("本地计算" if tool["cost_status"] == "not_charged" else "费用未知")).classes("muted")
            if self.service.settings.ifind_mcp_url:
                async def discover():
                    result = await self.service.discover_ifind()
                    ui.notify("工具发现：" + result["status"])
                    self.refresh_left()
                ui.button("刷新 iFinD 工具发现", on_click=discover).props("outline")
            ui.button("关闭", on_click=dialog.close).props("flat")
        dialog.open()

    def edit_plan_dialog(self):
        state = workspace_snapshot(self.service, self.owner, self.run_id)["run"]
        plan = (state.get("runtime") or {}).get("plan")
        if not plan or state["status"] not in {"paused", "awaiting_approval"}:
            ui.notify("请先暂停研究或等待确认后修改计划", type="warning")
            return
        locked = {c["task_id"] for c in state["tool_calls"]}
        physical = self.service.harness.physical_ids(self.run_id, plan)
        with self.client, ui.dialog() as dialog, ui.column().classes("surface dialog-card"):
            ui.label("修改未执行的研究计划").classes("panel-title")
            ui.label("已派发任务与证据保持原记录；保存会产生新版本并使旧审批失效，随后可继续研究。").classes("muted")
            editors = []
            for task in plan["tasks"]:
                with ui.column().classes("editor-task"):
                    if physical[task["id"]] in locked:
                        ui.label("已执行 · " + task["title"]).classes("font-semibold")
                        ui.label("该任务已有调用记录，不能修改或移除。").classes("muted")
                        editors.append((task, None))
                        continue
                    title = ui.input("任务标题", value=task["title"]).props("outlined dense").classes("w-full")
                    ui.label(next((t["description"] for t in self.service.tools() if t["name"] == task["tool"]), task["tool"])).classes("muted")
                    getter = self.arguments_editor(task["tool"], task["arguments"])
                    deps = ui.select({t["id"]: t["title"] for t in plan["tasks"][:plan["tasks"].index(task)]}, value=task["depends_on"], multiple=True, label="前置任务").props("outlined dense").classes("w-full")
                    removed = ui.checkbox("移除此未执行任务", value=False)
                    editors.append((task, (title, getter, deps, removed)))
            error = ui.label("").classes("text-negative text-sm")
            def save():
                try:
                    tasks = []
                    for original, editor in editors:
                        if editor is None:
                            tasks.append(original)
                        elif not editor[3].value:
                            tasks.append({**original, "title": editor[0].value, "arguments": editor[1](), "depends_on": editor[2].value})
                    self.service.harness.edit_plan(self.owner, state["id"], PlanUpdate(plan_version=state["plan_version"], plan_fingerprint=fingerprint(plan), plan={**plan, "tasks": tasks}))
                    dialog.close()
                    self.refresh_left()
                    self.refresh_run()
                    ui.notify("计划已保存，点击继续研究执行")
                except ServiceError as exc:
                    error.set_text(exc.message)
                except Exception:
                    error.set_text("参数格式无效，请核对日期、代码、数值及证据引用。")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("保存计划", on_click=save).classes("btn-primary")
        dialog.open()

    def arguments_editor(self, name, args):
        args, values, converters = dict(args), {}, {}
        def text(key, title, default=""):
            values[key] = ui.input(title, value=args.get(key, default)).props("outlined dense").classes("w-full")
        def count(key, title, default, minimum, maximum):
            values[key] = ui.number(title, value=args.get(key, default), min=minimum, max=maximum, precision=0).props("outlined dense").classes("w-full")
            converters[key] = int
        def choice(key, title, options, default):
            values[key] = ui.select(options, value=args.get(key, default), label=title).props("outlined dense").classes("w-full")
        def date(key, title, optional=False):
            value = args.get(key)
            values[key] = ui.input(title, value=datetime.fromtimestamp(value/1000, SHANGHAI).date().isoformat() if value else "").props("outlined dense").classes("w-full")
            converters[key] = lambda x: None if optional and not x else int(datetime.strptime(x, "%Y-%m-%d").replace(tzinfo=SHANGHAI).timestamp()*1000)
        if name == "resolve_security":
            text("q", "标的名称或代码")
            count("limit", "最多匹配数量", 10, 1, 20)
        elif name == "get_daily_bars":
            text("thscode", "标的代码或计划引用")
            date("start", "历史开始日期（YYYY-MM-DD）")
            date("end", "历史结束日期（YYYY-MM-DD）")
            choice("adjust", "复权口径", {"forward": "前复权", "backward": "后复权", "none": "未复权"}, "forward")
        elif name == "get_price_snapshot":
            values["symbols"] = ui.input("标的代码（逗号分隔）", value=",".join(args.get("symbols", []))).props("outlined dense").classes("w-full")
            converters["symbols"] = lambda x: [s.strip() for s in x.split(",") if s.strip()]
        elif name == "get_financial_metrics":
            text("thscode", "标的代码或计划引用")
            choice("statement", "财报类型", {"income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}, "income")
            choice("period", "报告类型", {"annual": "年报", "quarterly": "季度报告（单季口径待复核）"}, "annual")
            if args.get("start") is not None:
                date("start", "报告期开始日期")
                date("end", "报告期结束日期")
            else:
                count("limit", "最近报告数量", 4, 1, 20)
        elif name.startswith("calculate_"):
            values["evidence_ids"] = ui.input("来源任务引用（逗号分隔）", value=",".join(args.get("evidence_ids", []))).props("outlined dense").classes("w-full")
            converters["evidence_ids"] = lambda x: [s.strip() for s in x.split(",") if s.strip()]
            if name == "calculate_price_comparison":
                text("calendar_evidence_id", "交易日历任务引用")
                count("trading_days", "交易日收盘观察值数量", 60, 3, 2520)
            else:
                values["fields"] = ui.select({k: FIELDS[k] for k in ("operating_income", "parent_holder_net_profit", "net_profit", "basic_eps")}, value=args.get("fields", ["operating_income", "parent_holder_net_profit"]), multiple=True, label="比较字段").props("outlined dense").classes("w-full")
                date("period_end_ms", "指定报告期（可选，YYYY-MM-DD）", optional=True)
                date("known_at_ms", "历史知识截止日（可选，YYYY-MM-DD）", optional=True)
        elif name != "get_trading_calendar":
            raw = ui.textarea("工具参数 JSON", value=json.dumps(args, ensure_ascii=False, indent=2)).props("outlined").classes("w-full")
            return lambda: json.loads(raw.value)
        return lambda: {**args, **{k: converters.get(k, lambda x: x)(control.value) for k, control in values.items()}}

    def manual_dialog(self):
        with self.client, ui.dialog() as dialog, ui.column().classes("surface dialog-card"):
            ui.label("手动查询并保存证据").classes("panel-title")
            tools = {t["name"]: t["description"] for t in self.service.tools() if t["provider"] == "fuyao"}
            selected = ui.select(tools, value="get_daily_bars", label="查询内容").props("outlined").classes("w-full")
            area, getter = ui.column().classes("w-full"), {"value": None}
            today = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
            def form():
                area.clear()
                defaults = {"get_daily_bars": {"thscode": "600519.SH", "start": int((today-timedelta(days=160)).timestamp()*1000), "end": int(today.timestamp()*1000)}, "get_price_snapshot": {"symbols": ["600519.SH"]},
                    "get_financial_metrics": {"thscode": "600519.SH", "statement": "income", "period": "annual", "limit": 4}, "resolve_security": {"q": "600519.SH", "limit": 10}, "get_trading_calendar": {}}
                with area:
                    getter["value"] = self.arguments_editor(selected.value, defaults[selected.value])
            selected.on_value_change(lambda _: form())
            form()
            error = ui.label("").classes("text-negative text-sm")
            async def execute():
                button.disable()
                error.set_text("正在查询…")
                try:
                    args = getter["value"]()
                    self.service.arguments(selected.value, args)
                    if not self.thread_id:
                        self.thread_id = self.service.create_thread(self.owner, "手动金融研究", "")["id"]
                    run = self.service.create_run(self.owner, self.thread_id, "手动查询：" + tools[selected.value])
                    self.run_id = run["id"]
                    call = await self.service.execute(self.owner, run["id"], selected.value, args)
                    with self.service.db.connect() as conn:
                        conn.execute("UPDATE runs SET status=?,updated_at=? WHERE id=?", ("completed" if call["quality"] == "available" else "partial", now(), run["id"]))
                        self.service.db.event(conn, self.owner, run["id"], "run.finished", {"status": call["status"], "mode": "manual"})
                    self.refresh_left()
                    self.refresh_run()
                    self.remember()
                    dialog.close()
                    ui.notify("查询已保存：" + LABELS.get(call["quality"], call["quality"]))
                except ServiceError as exc:
                    error.set_text(exc.message)
                except Exception:
                    error.set_text("参数或日期格式无效，请检查输入。")
                finally:
                    button.enable()
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=dialog.close).props("flat")
                button = ui.button("查询并保存证据", on_click=execute).classes("btn-primary")
        dialog.open()


def register_workbench(service):
    @ui.page("/")
    def index(request: Request):
        Workbench(service, session_owner(request))
