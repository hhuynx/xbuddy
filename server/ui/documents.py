import asyncio
from io import BytesIO
from nicegui import ui
from server.services import ServiceError


def document_dialog(workbench):
    documents = workbench.service.documents
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
        ui.label('研究资料').classes('panel-title')
        ui.label('导入年报或公告原文，研究时可检索并引用出处。原文陈述仍需核验；PDF 页码按文件页序。').classes('muted')
        title = ui.input('资料标题').classes('w-full')
        source = ui.input('原文 HTTPS 链接').classes('w-full')
        published = ui.input('发布时间（带时区）',placeholder='2026-03-31T09:00:00+08:00').classes('w-full')
        body = ui.textarea('正文（可粘贴，或上传 TXT / Markdown / 可提取文字的 PDF）').props('outlined').classes('w-full')
        pages = []

        async def uploaded(event):
            try:
                data = await event.file.read()
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError()
                name = event.file.name.casefold()
                if name.endswith('.pdf'):
                    def extract():
                        from pypdf import PdfReader
                        reader = PdfReader(BytesIO(data))
                        if len(reader.pages) > 500:
                            raise ValueError()
                        return [p.extract_text() or '' for p in reader.pages]
                    extracted = await asyncio.to_thread(extract)
                elif name.endswith(('.txt','.md')):
                    extracted = [data.decode('utf-8-sig')]
                else:
                    raise ValueError()
                text = '\n'.join(extracted)
                if not text.strip() or len(text) > 2_000_000:
                    raise ValueError()
                pages[:] = extracted
                body.set_value(text)
                if not title.value:
                    title.set_value(event.file.name)
            except Exception:
                ui.notify('无法读取文字，请使用 UTF-8 文本或带文字层的 PDF（最多 2 MB）；扫描件请先转文字。',type='warning')

        ui.upload(on_upload=uploaded,max_file_size=2*1024*1024,auto_upload=True).props('accept=".txt,.md,.pdf"').classes('w-full')
        listing = ui.column().classes('w-full')

        def refresh():
            listing.clear()
            with listing:
                for item in documents.listing(workbench.owner):
                    ui.link(item['title'],item['source_url'],new_tab=True)
                    ui.label(item['published_at']).classes('muted')

        def save():
            try:
                documents.import_text(workbench.owner,title.value or '',source.value or '',published.value or '',body.value or '',
                    pages=pages if '\n'.join(pages)==body.value else None)
                refresh()
                ui.notify('资料已导入，可在对话中要求基于原文研究。',type='positive')
            except ServiceError as error:
                ui.notify(error.message,type='warning')

        ui.button('导入资料',on_click=save).classes('btn-primary')
        query = ui.input('检索原文').classes('w-full')
        results = ui.column().classes('w-full')

        def search():
            results.clear()
            try:
                found = documents.search(workbench.owner,query.value or '')
                with results:
                    if not found:ui.label('没有匹配的原文片段。')
                    for item in found:
                        ui.link(item['title'],item['source_url'],new_tab=True)
                        ui.label(f"{item['published_at']} · 第 {item['page']} 页 · 字符 {item['offset_start']}–{item['offset_end']}").classes('muted')
                        ui.label(item['text']).classes('whitespace-pre-wrap')
            except ServiceError as error:
                ui.notify(error.message,type='warning')
        ui.button('检索',on_click=search).props('outline')
        ui.button('关闭',on_click=dialog.close).props('flat')
        refresh()
    dialog.open()
