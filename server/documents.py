"""Bounded original-text retrieval; no download, embedding service or invented facts."""
import hashlib
import re
from datetime import datetime
from urllib.parse import urlparse
from server.storage.database import fingerprint, now, uid


def tokens(text):
    text = text.casefold()
    words = set(re.findall(r'[a-z0-9_]{2,}', text))
    for part in re.findall(r'[\u4e00-\u9fff]+', text):
        words.update(part[i:i+2] for i in range(max(1,len(part)-1)))
    return words


class Documents:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def import_text(self, owner, title, source_url, published_at, text, pages=None):
        from server.services import ServiceError
        self.service.ensure_owner(owner)
        try:
            when = datetime.fromisoformat(published_at.replace('Z','+00:00'))
            if when.tzinfo is None:
                raise ValueError()
            if not 1 <= len(title.strip()) <= 200 or not 1 <= len(text.strip()) <= 2_000_000:
                raise ValueError()
            parsed = urlparse(source_url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError()
            if len(source_url) > 1500:
                raise ValueError()
        except ValueError:
            raise ServiceError(422,'需要标题、HTTPS 原文链接、带时区的发布时间和非空正文（最多两百万字符）') from None
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = self.db.one('SELECT id FROM research_documents WHERE session_id=? AND content_sha256=? AND source_url=? AND published_at=?',
            (owner,digest,source_url,published_at))
        if old:
            return {'id':old['id'],'reused':True}
        total = self.db.one('SELECT COUNT(*) AS n FROM research_documents WHERE session_id=?',(owner,))['n']
        if total >= 100:
            raise ServiceError(409,'最小版每个会话最多保存一百份文档')
        identifier = uid()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT id FROM research_documents WHERE session_id=? AND content_sha256=? AND source_url=? AND published_at=?',
                (owner,digest,source_url,published_at)).fetchone()
            if old:return {'id':old['id'],'reused':True}
            if conn.execute('SELECT COUNT(*) AS n FROM research_documents WHERE session_id=?',(owner,)).fetchone()['n'] >= 100:
                raise ServiceError(409,'最小版每个会话最多保存一百份文档')
            conn.execute('INSERT INTO research_documents VALUES(?,?,?,?,?,?,?,?)',
                (identifier,owner,title.strip(),source_url,published_at,text,digest,now()))
            offset = 0
            for page, body in enumerate(pages or [text], 1):
                for start in range(0,len(body),850):
                    excerpt = body[start:start+1000]
                    conn.execute('INSERT INTO document_chunks VALUES(?,?,?,?,?,?,?)',
                        (uid(),identifier,page,offset+start,offset+start+len(excerpt),excerpt,fingerprint(excerpt)))
                offset += len(body)+1
        return {'id':identifier,'reused':False}

    def listing(self, owner):
        self.service.ensure_owner(owner)
        return self.db.all('SELECT id,title,source_url,published_at,content_sha256,created_at FROM research_documents WHERE session_id=? ORDER BY created_at DESC',(owner,))

    def search(self, owner, query, limit=5, document_ids=None):
        self.service.ensure_owner(owner)
        from server.services import ServiceError
        if not 1 <= len(query.strip()) <= 300 or not 1 <= limit <= 8:
            raise ServiceError(422,'检索词需为一至三百字符，结果上限为八段')
        wanted = tokens(query)
        ranked = []
        for document in self.listing(owner):
            if document_ids and document['id'] not in document_ids:
                continue
            for chunk in self.db.all('SELECT * FROM document_chunks WHERE document_id=?',(document['id'],)):
                score = len(wanted & tokens(chunk['text']))
                if score:
                    row = {**chunk,**{k:document[k] for k in ('title','source_url','published_at')},'document_sha256':document['content_sha256']}
                    ranked.append((score,row))
                    ranked.sort(key=lambda x:(-x[0],x[1]['document_id'],x[1]['offset_start']))
                    del ranked[limit:]
        result = [{'document_id':r['document_id'],'chunk_id':r['id'],'title':r['title'],'source_url':r['source_url'],
            'published_at':r['published_at'],'page':r['page'],'offset_start':r['offset_start'],'offset_end':r['offset_end'],
            'text':r['text'],'content_sha256':r['content_sha256'],'document_sha256':r['document_sha256']}
            for _,r in ranked]
        try:
            for item in result:self.verify_excerpt(owner,item)
        except ValueError:
            raise ServiceError(409,'文档完整性检查失败，请重新导入原文') from None
        return result

    def verify_excerpt(self, owner, item):
        row = self.db.one('SELECT c.*,d.body,d.content_sha256 AS document_hash,d.source_url,d.published_at,d.title '
            'FROM document_chunks c JOIN research_documents d ON d.id=c.document_id WHERE c.id=? AND d.session_id=?',
            (item['chunk_id'],owner))
        if not row or any(item[k] != row[k] for k in ('document_id','page','offset_start','offset_end','text','source_url','published_at','title')):
            raise ValueError('document_scope_or_provenance')
        if (fingerprint(row['text']) != item['content_sha256'] or fingerprint(row['text']) != row['content_sha256'] or
            hashlib.sha256(row['body'].encode()).hexdigest() != item['document_sha256'] or row['document_hash'] != item['document_sha256'] or
            row['body'][row['offset_start']:row['offset_end']] != row['text']):
            raise ValueError('document_hash_or_original_position')
