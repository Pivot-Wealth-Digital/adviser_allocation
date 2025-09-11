(function(){
  const $ = (sel, root=document) => root.querySelector(sel);
  const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

  const submit = $('#submit');
  const s = $('#start');
  const e = $('#end');
  const d = $('#description');
  const tagSelect = $('#tagSelect');
  const msg = $('#msg');
  const table = $('#closures-table');

  function show(type, text){
    msg.className = 'msg ' + type;
    msg.textContent = text;
    msg.style.display = 'block';
    setTimeout(()=>{ msg.style.display = 'none'; }, 1800);
  }

  function toDate(s){ const t=new Date((s||'')+'T00:00:00'); return isNaN(t)?null:t; }
  function workdaysJS(sv,ev){ const sd=toDate(sv), ed=toDate(ev||sv); if(!sd||!ed)return 0; let sN=sd.getTime(), eN=ed.getTime(); if(eN<sN){[sN,eN]=[eN,sN];} let d=0; for(let t=sN;t<=eN;t+=86400000){ const wd=(new Date(t)).getDay(); if(wd>=1&&wd<=5)d++; } return d; }
  function renderLiveWD(){ const sv=s.value, ev=e.value||sv; const n=workdaysJS(sv,ev); const el=$('#livewd'); if(!el) return; el.textContent = sv?(`Workdays: ${n} (Mon–Fri)`):''; }
  if(s) s.addEventListener('change', renderLiveWD);
  if(e) e.addEventListener('change', renderLiveWD);
  renderLiveWD();

  // (Clean) no extra calendar popovers

  // Calendar utilities and popover attach
  function daysInMonth(year, month){ return new Date(year, month+1, 0).getDate(); }
  function firstDow(year, month){ return new Date(year, month, 1).getDay(); }
  function ymd(dt){ const m = (dt.getMonth()+1).toString().padStart(2,'0'); const d = dt.getDate().toString().padStart(2,'0'); return dt.getFullYear()+"-"+m+"-"+d; }
  function buildDateSet(closures){ const map={}; (closures||[]).forEach(c=>{const s=new Date(c.start_date+"T00:00:00"), e=new Date((c.end_date||c.start_date)+"T00:00:00"); for(let t=s; t<=e; t=new Date(t.getFullYear(),t.getMonth(),t.getDate()+1)){ const key=ymd(t); if(!map[key]) map[key]=c.color||'blue'; }}); return map; }
  function renderCalendarInto(el, closures, dateStr){ if(!el) return; const base=new Date((dateStr||'')+"T00:00:00"); const y=base.getFullYear(), m=base.getMonth(); const total=daysInMonth(y,m), dow0=firstDow(y,m); const dateColors=buildDateSet(closures); const dows=['S','M','T','W','T','F','S']; let html=''; for(const d of dows){ html += '<div class="dow">'+d+'</div>'; } for(let i=0;i<dow0;i++){ html+='<div class="day muted"></div>'; } for(let d=1; d<=total; d++){ const ds=y+"-"+(m+1+"" ).padStart(2,'0')+"-"+(d+"" ).padStart(2,'0'); const cls=dateColors[ds]?(' cal-'+dateColors[ds]):''; html+='<div class="day'+cls+'">'+d+'</div>'; } el.innerHTML=html; }
  function placePopover(trigger, pop){ const r=trigger.getBoundingClientRect(); pop.style.left=(r.left+window.scrollX)+'px'; pop.style.top=(r.bottom+window.scrollY+6)+'px'; }
  function attachCalendarPopover(triggerId, popId, calId, targetInputId, closures, defaultDate){ const trigger=document.getElementById(triggerId), pop=document.getElementById(popId), cal=document.getElementById(calId), target= targetInputId?document.getElementById(targetInputId):null; if(!trigger||!pop||!cal) return; let open=false; function openPop(){ placePopover(trigger,pop); pop.style.display='block'; open=true; renderCalendarInto(cal, closures, (target&&target.value)||defaultDate); } function closePop(){ pop.style.display='none'; open=false; } trigger.addEventListener('click',function(e){ e.stopPropagation(); open?closePop():openPop(); }); document.addEventListener('click',function(e){ if(open && !pop.contains(e.target) && e.target!==trigger){ closePop(); }}); cal.addEventListener('click',function(e){ const dEl=e.target.closest('.day'); if(!dEl||!target) return; const base=new Date(((target&&target.value)||defaultDate)+"T00:00:00"); const m=(base.getMonth()+1+"" ).padStart(2,'0'); const d=(dEl.textContent||'' ).padStart(2,'0'); const y=base.getFullYear(); if(d){ target.value=y+'-'+m+'-'+d; closePop(); renderLiveWD(); } }); }
  // Attach for topbar and start field
  try{ if(window.CLOSURES_FOR_JS){ const todayInputEl=document.getElementById('todayInput')||document.querySelector('.datebox input'); const todayStr=(todayInputEl&&todayInputEl.value)||''; attachCalendarPopover('calTriggerTop','calPopoverTop','mini-cal','todayInput', window.CLOSURES_FOR_JS, todayStr); attachCalendarPopover('calTriggerStart','calPopoverStart','mini-cal-start','start', window.CLOSURES_FOR_JS, todayStr); } }catch(_){ }

  if(submit){
    submit.addEventListener('click', async ()=>{
      const start=s.value, end=e.value||start, description=d.value||''; const cur=(tagSelect&&tagSelect.value||'').trim(); const tags = cur?[cur]:[];
      if(!start){ show('err','Start date is required'); return; }
      try{
        const res = await fetch('/closures',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({start_date:start,end_date:end,description:description,tags:tags})});
        const data = await res.json();
        if(res.status===401){ location.href='/admin/login?next=/closures/ui'; return; }
        if(!res.ok){ show('err', data.error||'Failed to create'); return; }
        show('ok','Created');
        location.reload();
      }catch(err){ show('err','Network error'); }
    });
  }

  if(table){
    table.addEventListener('click', async (ev)=>{
      const btn = ev.target;
      const tr = btn.closest ? btn.closest('tr') : null; if(!tr) return;
      const id = tr.dataset.id;
      if(btn.classList.contains('edit')){
        const sCell = tr.querySelector('.start');
        const eCell = tr.querySelector('.end');
        const dCell = tr.querySelector('.description');
        const sVal = (sCell.textContent||'').trim();
        const eVal = (eCell.textContent||'').trim();
        const dVal = ((dCell.querySelector('.desc-text')||{}).textContent||dCell.textContent||'').trim();
        sCell.innerHTML = `<input type="date" value="${sVal}" />`;
        eCell.innerHTML = `<input type="date" value="${eVal}" />`;
        dCell.innerHTML = `<div class="row"><select class="tag-select"><option value="">-- Select tag --</option><option>Public Holiday</option><option>Wellness Day</option><option>Office Maintenance</option><option>Weather Event</option><option>National Holiday</option><option>Regional Holiday</option><option>System Maintenance</option><option>Office Closure</option></select></div><textarea rows="2" placeholder="Description">${dVal.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</textarea>`;
        tr.querySelector('.edit').style.display='none';
        tr.querySelector('.delete').style.display='none';
        tr.querySelector('.save').style.display='inline-block';
        tr.querySelector('.cancel').style.display='inline-block';
        return;
      }
      if(btn.classList.contains('cancel')){ location.reload(); return; }
      if(btn.classList.contains('save')){
        const sVal = (tr.querySelector('.start input')||{}).value;
        const eVal = (tr.querySelector('.end input')||{}).value || sVal;
        const dVal = ((tr.querySelector('.description textarea')||{}).value||'');
        const tagSel = (tr.querySelector('.description .tag-select')||{}).value||'';
        const tags = tagSel ? [tagSel] : [];
        try{
          const res = await fetch('/closures/'+id,{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({start_date:sVal,end_date:eVal,description:dVal,tags:tags})});
          const data = await res.json();
          if(res.status===401){ location.href='/admin/login?next=/closures/ui'; return; }
          if(!res.ok){ show('err', data.error||'Failed to update'); return; }
          show('ok','Updated');
          location.reload();
        }catch(err){ show('err','Network error'); }
        return;
      }
      if(btn.classList.contains('delete')){
        if(!confirm('Delete this closure?')) return;
        try{
          const res = await fetch('/closures/'+id,{method:'DELETE',credentials:'same-origin',headers:{'Accept':'application/json'}});
          if(res.status===401){ location.href='/admin/login?next=/closures/ui'; return; }
          if(!res.ok){ show('err','Failed to delete'); return; }
          show('ok','Deleted');
          location.reload();
        }catch(err){ show('err','Network error'); }
        return;
      }
    });
  }
})();
