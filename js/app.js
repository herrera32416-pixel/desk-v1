function normalize(data){
  // Accept desk-pwa/v1 (Sports Betting) and flat Strawhat draft
  const card = data.card || {};
  const clears = (card.clears || data.clears || []).map(p=>({
    ...p,
    edge: p.edge ?? p.edge_pct ?? '—',
    clears: p.tag==='CLEAR' || p.status==='WRITTEN' || p.clears===true,
    units: p.units ?? 0,
  }));
  const fills = (card.fills || data.fills || []).map(p=>({
    ...p,
    edge: p.edge ?? p.edge_pct ?? '—',
    clears: false,
    units: p.units ?? 0,
  }));
  const parlays = data.parlays || data.tickets?.parlays || [];
  const teasers = data.teasers || data.tickets?.teasers || [];
  const rulesObj = data.rules;
  let rules=[];
  if(Array.isArray(rulesObj)) rules=rulesObj;
  else if(rulesObj && typeof rulesObj==='object'){
    rules=Object.values(rulesObj);
  }
  return {
    slate_date: data.slate_date || data.slate_date_ct || '',
    timezone: data.timezone || 'America/Chicago',
    as_of_label: data.as_of_label || (data.generated_at_ct||'').replace('T',' ').slice(0,19) || '',
    summary: {
      published_clear: data.summary?.published_clear ?? clears.length,
      lean: data.summary?.lean || 'none',
      sports: data.summary?.sports || ['MLB','NFL','CFB','SOC'],
    },
    clears, fills, parlays, teasers, rules,
    ledger: data.ledger || null,
  };
}
async function loadDesk(){
  const res=await fetch('./data/today.json?ts='+Date.now(),{cache:'no-store'});
  if(!res.ok) throw new Error('No slate');
  return normalize(await res.json());
}
function el(tag, cls, text){
  const n=document.createElement(tag);
  if(cls) n.className=cls;
  if(text!=null) n.textContent=text;
  return n;
}
function playCard(p){
  const a=el('article','play');
  a.dataset.sport=p.sport||'OTHER';
  const hd=el('div','play-hd');
  hd.append(el('span','pill',p.tag||'CLEAR'), el('span','when',`${p.sport||''} · ${p.units||0}u`));
  a.append(hd);
  a.append(el('h3','serif',p.matchup||`${p.away} at ${p.home}`));
  a.append(el('div','side',p.selection||''));
  const m=el('div','metrics');
  const model = p.model_win_pct!=null ? `${p.model_win_pct}%` : '—';
  const market = p.market_win_pct!=null ? `${p.market_win_pct}%` : '—';
  const edge = (p.edge ?? p.edge_pct);
  const edgeStr = edge!=null && edge!=='' ? String(edge) : '—';
  const cells=[['MODEL', model],['MARKET', market],['EDGE', edgeStr]];
  for(const [k,v] of cells){
    const d=el('div');
    d.append(el('div','k',k), el('div','v serif',String(v)));
    m.append(d);
  }
  a.append(m);
  let why=p.notes||'';
  if(p.colab_win_pct!=null) why = `Colab ${p.colab_win_pct}%` + (p.colab_model_line!=null?` · FPI ${p.colab_model_line}`:'') + (why?` · ${why}`:'');
  if(why) a.append(el('p','why',why));
  return a;
}
function ticketCard(t, kind){
  const a=el('article','play');
  const hd=el('div','play-hd');
  const price=t.price || t.combined || '';
  hd.append(el('span','pill',t.tag||kind), el('span','when', kind+(price?` · ${price}`:'')));
  a.append(hd);
  a.append(el('h3','serif',t.id||t.name||kind));
  let legs=t.legs||[];
  if(legs.length && typeof legs[0]==='object'){
    legs=legs.map(l=>l.selection||l.side||l.label||JSON.stringify(l));
  }
  const side=el('div','side',legs.join('\n'));
  side.style.whiteSpace='pre-line';
  a.append(side);
  return a;
}
function showPanel(id){
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on', p.id==='p-'+id));
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on', b.dataset.p===id));
}
function filterSport(s){
  document.querySelectorAll('#clears .play, #fills .play').forEach(c=>{
    c.style.display=(s==='ALL'||c.dataset.sport===s)?'':'none';
  });
}
async function main(){
  const data=await loadDesk();
  document.getElementById('asof').textContent=`${data.slate_date}\n${data.timezone}\n${data.as_of_label||''}`;
  document.getElementById('clearCount').textContent=String(data.summary?.published_clear ?? data.clears?.length ?? 0);
  document.getElementById('leanVal').textContent=data.summary?.lean || 'none';
  document.getElementById('asOfFoot').textContent=`As of ${data.as_of_label||''} · sports ${(data.summary?.sports||[]).join(' · ')}`;

  const clears=document.getElementById('clears');
  clears.innerHTML='';
  (data.clears||[]).forEach(p=>clears.append(playCard(p)));
  if(!(data.clears||[]).length) clears.append(el('p','why','No CLEAR tickets today.'));

  const fills=document.getElementById('fills');
  fills.innerHTML='';
  (data.fills||[]).forEach(p=>fills.append(playCard(p)));

  const parlays=document.getElementById('parlays');
  parlays.innerHTML='';
  (data.parlays||[]).forEach(t=>parlays.append(ticketCard(t,'PARLAY')));
  if(!(data.parlays||[]).length) parlays.append(el('p','why','No parlays in today’s pack.'));

  const teasers=document.getElementById('teasers');
  teasers.innerHTML='';
  (data.teasers||[]).forEach(t=>teasers.append(ticketCard(t,'TEASER')));
  if(!(data.teasers||[]).length) teasers.append(el('p','why','No teasers in today’s pack.'));

  const rules=document.getElementById('rules');
  rules.innerHTML='';
  (data.rules||[]).forEach((r,i)=>{
    const d=el('div','rule');
    const b=el('b',null,`G${i+1}`);
    d.append(b, document.createTextNode(' '+r));
    rules.append(d);
  });

  if(data.ledger){
    document.getElementById('writtenBook').textContent=data.ledger.written_book||'—';
    document.getElementById('friExam').textContent=data.ledger.fri_exam||'—';
    document.getElementById('friWritten').textContent=data.ledger.fri_written||'CLEAR results only.';
  }

  document.getElementById('status').textContent='Live · pull to refresh';
}
document.getElementById('tabs').addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return; showPanel(b.dataset.p);
});
document.getElementById('chips').addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  document.querySelectorAll('#chips button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); filterSport(b.dataset.s);
});
document.getElementById('refresh').onclick=()=>main().catch(err=>{
  document.getElementById('status').textContent='Offline / no slate';
  console.error(err);
});
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('./sw.js').catch(()=>{});
}
main().catch(err=>{
  document.getElementById('status').textContent='Could not load today.json';
  console.error(err);
});
