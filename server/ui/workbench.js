/* Preserve the reading position when new messages arrive or history is prepended. */
window.xbuddyChat = (() => {
  let area, list, button, observer, resizeObserver;
  let follow=true, unread=false, prepend=null, count=0, frame=0;
  const bottom=()=>area && area.scrollHeight-area.scrollTop-area.clientHeight<90;
  function badge(){
    if(!button || !area || !area.clientHeight)return;
    button.classList.toggle('visible',!bottom());
    button.classList.toggle('has-unread',unread);
  }
  function jump(){follow=true;unread=false;if(area && area.clientHeight)area.scrollTop=area.scrollHeight;badge();}
  function flush(){
    cancelAnimationFrame(frame);
    frame=requestAnimationFrame(()=>requestAnimationFrame(()=>{
      if(!area)return;
      const next=list.querySelectorAll('.chat-message').length;
      if(prepend){area.scrollTop=prepend.top+area.scrollHeight-prepend.height;follow=prepend.follow;prepend=null;}
      else if(follow)jump();else if(next>count)unread=true;
      count=next;badge();
    }));
  }
  function bind(){
    const next=document.querySelector('.chat-scroll');if(!next)return;
    if(area===next){flush();return;}
    observer?.disconnect();resizeObserver?.disconnect();
    area=next;list=document.querySelector('.conversation-box');button=document.querySelector('.latest-button');
    count=list.querySelectorAll('.chat-message').length;
    area.addEventListener('scroll',()=>{if(!area.clientHeight || prepend)return;follow=bottom();if(follow)unread=false;badge();},{passive:true});
    observer=new MutationObserver(flush);observer.observe(list,{childList:true,subtree:true,characterData:true});
    resizeObserver=new ResizeObserver(()=>{if(follow && !prepend)flush();});resizeObserver.observe(document.querySelector('.chat-content'));
    window.addEventListener('resize',flush);jump();
  }
  function beforePrepend(){if(area)prepend={top:area.scrollTop,height:area.scrollHeight,follow};}
  function reset(){follow=true;unread=false;prepend=null;count=0;flush();}
  function toggleSidebar(){document.body.classList.toggle(innerWidth<900?'sidebar-open':'sidebar-collapsed');}
  document.addEventListener('keydown',e=>{if(e.key==='Escape')document.body.classList.remove('sidebar-open');});
  return {bind,jump,beforePrepend,reset,toggleSidebar};
})();
