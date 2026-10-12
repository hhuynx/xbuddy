"""OpenTelemetry spans, persisted locally for the research process UI. No prompts or keys."""
import time
from functools import wraps
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode
from server.storage.database import encode, now, uid

provider = TracerProvider()
tracer = provider.get_tracer('xbuddy.harness')


def traced(name):
    def decorate(function):
        @wraps(function)
        async def invoke(self, *args, **kwargs):
            if isinstance(args[0], dict):
                state = args[0]
                run_id, owner = state.get('run_id',state.get('id')), state.get('owner',state.get('session_id'))
            else:
                owner, run_id = args[:2]
            attrs = {'run_id':run_id}
            if name == 'tool':
                attrs['tool'] = args[2]
            if name == 'model':
                attrs['phase'] = args[1]
            started, clock, outcome = now(), time.perf_counter(), 'completed'
            with tracer.start_as_current_span(name, attributes=attrs, record_exception=False, set_status_on_exception=False) as span:
                try:
                    result = await function(self,*args,**kwargs)
                    if name == 'research':
                        row = self.db.one('SELECT status FROM runs WHERE id=?',(run_id,))
                        attrs['run_status'] = row['status'] if row else 'unknown'
                        if attrs['run_status'] in {'failed','recoverable_failed'}:
                            outcome = 'failed'
                            span.set_status(Status(StatusCode.ERROR,attrs['run_status']))
                        elif attrs['run_status'] in {'awaiting_approval','awaiting_input','paused','stopped'}:
                            outcome = attrs['run_status']
                    if name == 'tool':
                        attrs.update(quality=result.get('quality','unknown'),attempts=result.get('attempts',0),reused=result.get('reused',False))
                    if name == 'tool' and result.get('status') != 'completed':
                        outcome = 'failed'
                        span.set_status(Status(StatusCode.ERROR,'tool_failed'))
                    return result
                except BaseException as error:
                    expected = type(error).__name__ in {'CancelledError','GraphInterrupt'}
                    outcome = 'interrupted' if expected else 'failed'
                    attrs['error_type'] = type(error).__name__
                    if not expected:
                        span.set_status(Status(StatusCode.ERROR,attrs['error_type']))
                    raise
                finally:
                    ctx = span.get_span_context()
                    attrs['span_id'] = format(ctx.span_id,'016x')
                    # A failed trace write must never replace the actual model/tool result.
                    try:
                        with self.db.connect() as conn:
                            conn.execute('INSERT INTO execution_spans VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                (uid(),owner,run_id,format(ctx.trace_id,'032x'),format(span.parent.span_id,'016x') if span.parent else None,
                                    name,outcome,round((time.perf_counter()-clock)*1000),encode(attrs),started,now()))
                    except Exception:
                        pass
        return invoke
    return decorate


def trace_view(service, owner, run_id):
    service.resource('runs',run_id,owner)
    from server.services import view
    spans = [view(r) for r in service.db.all('SELECT * FROM execution_spans WHERE run_id=? AND session_id=? ORDER BY started_at LIMIT 1000',(run_id,owner))]
    usage = [view(r) for r in service.db.all('SELECT phase,status,usage_json,elapsed_ms FROM model_calls WHERE run_id=? AND session_id=? ORDER BY started_at',(run_id,owner))]
    attempts = service.db.all('SELECT a.call_id,a.attempt,a.elapsed_ms,a.error,a.http_status FROM tool_attempts a '
        'JOIN tool_calls c ON c.id=a.call_id WHERE c.run_id=? AND c.session_id=? ORDER BY a.started_at',(run_id,owner))
    dispatch = service.harness.dispatch.status if service.settings.embedded_worker else 'external_worker'
    return {'spans':spans,'model_usage':usage,'tool_attempts':attempts,'dispatch':dispatch}
