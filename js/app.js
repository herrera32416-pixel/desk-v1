let deskMode = 'plays';
let deskSport = 'ALL';
let deskData = null;

function normalize(data){
  const card = data.card || {};
  const clears = (card.clears || data.clears || []).map(p=>({
    ...p,
    edge: p.edge ?? p.edge_pct ?? '—',
    tag: p.tag || 'CLEAR',
    clears: true,
    units: p.units ?? 0,
  }));
  const fills = (card.fills || data.fills || []).map(p=>({
    ...p,
    edge: p.edge ?? p.edge_pct ?? '—',
    tag: p.tag || 'FILL',
    clears: false,
    units: p.units ?? 0,
  }));
  const holds = (card.holds || data.holds || []).map(p=>({
    ...p,
    edge: p.edge ?? p.edge_pct ?? '—',
    tag: p.tag || 'HOLD',
    clears: false,
    units: p.units ?? 0,
  }));
  const props = (card.props || data.props || []).map(p=>({
    ...p,
    tag: p.tag || ((p.clears===true || String(p.clears).toUpperCase()==='Y') ? 'CLEAR' : 'FILL'),
    sport: p.sport || 'NFL',
    units: p.units ?? 0,
  }));
  const parlays = data.parlays || data.tickets?.parlays || [];
  const teasers = data.teasers || data.tickets?.teasers || [];
  const rulesObj = data.rules;
  let rules=[];
  if(Array.isArray(rulesObj)) rules=rulesObj;
  else if(rulesObj && typeof rulesObj==='object') rules=Object.values(rulesObj);
  return {
    slate_date: data.slate_date || data.slate_date_ct || '',
    timezone: data.timezone || 'America/Chicago',
    as_of_label: data.as_of_label || (data.generated_at_ct||'').replace('T',' ').slice(0,19) || '',
    summary: {
      published_clear: data.summary?.published_clear ?? clears.length,
      lean: data.summary?.lean || 'none',
      sports: data.summary?.sports || data.summary?.by_sport && Object.keys(data.summary.by_sport) || ['MLB','NFL','CFB','SOC'],
      props_total: data.summary?.props_total ?? props.length,
      props_clear: data.summary?.props_clear ?? props.filter(p=>p.tag==='CLEAR').length,
    },
    clears, fills, holds, props, parlays, teasers, rules,
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
function playCard(p, {star}={}){
  const a=el('article','play'+(star||p.tag==='CLEAR'?' clear-play':''));
  a.dataset.sport=p.sport||'OTHER';
  a.dataset.kind=p.kind||'side';
  const hd=el('div','play-hd');
  const pill=el('span','pill'+(star?' star':''), star?'PLAY':(p.tag||'CLEAR'));
  hd.append(pill, el('span','when',`${p.sport||''} · ${p.units||0}u`));
  a.append(hd);
  a.append(el('h3','serif',p.matchup||`${p.away||''} at ${p.home||''}`));
  a.append(el('div','side',p.selection|| (p.tag==='HOLD'?'Hold — no number':'')));
  const m=el('div','metrics');
  const model = p.model_win_pct!=null ? `${p.model_win_pct}%` : '—';
  const market = p.market_win_pct!=null ? `${p.market_win_pct}%` : '—';
  const edge = (p.edge ?? p.edge_pct);
  const edgeStr = edge!=null && edge!=='' ? String(edge) : '—';
  for(const [k,v] of [['MODEL', model],['MARKET', market],['EDGE', edgeStr]]){
    const d=el('div');
    d.append(el('div','k',k), el('div','v serif',String(v)));
    m.append(d);
  }
  a.append(m);
  let why=p.notes||'';
  if(p.book || p.price_american){
    const shop=[p.book, p.price_american!=null?String(p.price_american):''].filter(Boolean).join(' ');
    why = why ? `${shop} · ${why}` : shop;
  }
  if(why) a.append(el('p','why',why));
  return a;
}
function propCard(p){
  const a=el('article','play'+(p.tag==='CLEAR'?' clear-play':''));
  a.dataset.sport=p.sport||'NFL';
  a.dataset.kind='prop';
  const hd=el('div','play-hd');
  const st=p.prop_status||p.tag||'PROP'; const pill=el('span','pill'+(st==='CLEAR'||st==='OPEN'?' star':''), st==='CLEAR'?'PROP PLAY':st);
  hd.append(pill, el('span','when',`${p.sport||''} · ${p.units||0}u`));
  a.append(hd);
  const title=p.player || p.selection || 'Player prop';
  a.append(el('h3','serif',title));
  const lineBits=[p.market, p.side, p.line!=null && p.line!==''?String(p.line):''].filter(Boolean).join(' · ');
  const game=p.game || p.matchup || [p.team,p.opponent].filter(Boolean).join(' vs ');
  a.append(el('div','side', [lineBits, game].filter(Boolean).join('\n')));
  a.querySelector('.side').style.whiteSpace='pre-line';
  const m=el('div','metrics');
  const model = p.model_win_pct!=null ? `${p.model_win_pct}%` : '—';
  const market = p.market_win_pct!=null ? `${p.market_win_pct}%` : (p.price_american!=null?String(p.price_american):'—');
  const edge = p.edge_pct ?? p.edge;
  for(const [k,v] of [['MODEL', model],['PRICE', market],['EDGE', edge!=null && edge!==''?String(edge):'—']]){
    const d=el('div');
    d.append(el('div','k',k), el('div','v serif',String(v)));
    m.append(d);
  }
  a.append(m);
  if(p.notes || p.book) a.append(el('p','why', [p.book, p.notes].filter(Boolean).join(' · ')));
  return a;
}
function ticketCard(t, kind){
  const a=el('article','play');
  const hd=el('div','play-hd');
  const price=t.price || t.combined || t.combined_american_assume_110 || '';
  hd.append(el('span','pill',t.tag||kind), el('span','when', kind+(price?` · ${price}`:'')));
  a.append(hd);
  a.append(el('h3','serif',t.id||t.name||t.title||kind));
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
function sportMatch(p, s){
  return s==='ALL' || (p.sport||'')===s;
}
function renderSlate(){
  if(!deskData) return;
  const slate=document.getElementById('slate');
  const hint=document.getElementById('sportHint');
  slate.innerHTML='';
  let rows=[];
  if(deskMode==='plays'){
    rows=deskData.clears.filter(p=>sportMatch(p, deskSport));
    if(hint) hint.textContent = deskSport==='ALL' ? 'Recommended · CLEAR only' : `${deskSport} plays · CLEAR only`;
    if(!rows.length) slate.append(el('p','why','No recommended plays for this filter.'));
    else rows.forEach(p=>slate.append(playCard(p,{star:true})));
  } else {
    // board (default fallback)
    deskMode='board';
    rows=[...deskData.fills, ...deskData.holds].filter(p=>sportMatch(p, deskSport));
    if(hint) hint.textContent = deskSport==='ALL' ? 'Full board · FILL + HOLD' : `${deskSport} board · FILL + HOLD`;
    if(!rows.length) slate.append(el('p','why','No board games for this filter.'));
    else rows.forEach(p=>slate.append(playCard(p)));
  }
}
async function main(){
  deskData=await loadDesk();
  const data=deskData;
  document.getElementById('asof').textContent=`${data.slate_date}\n${data.timezone}\n${data.as_of_label||''}`;
  document.getElementById('clearCount').textContent=String(data.summary?.published_clear ?? data.clears?.length ?? 0);
  const bc=document.getElementById('boardCount');
  if(bc) bc.textContent=String((data.fills?.length||0)+(data.holds?.length||0));
  document.getElementById('asOfFoot').textContent=`As of ${data.as_of_label||''} · ${data.clears.length} plays`;

  renderSlate();

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
    d.append(el('b',null,`G${i+1}`), document.createTextNode(' '+r));
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
  b.classList.add('on'); deskSport=b.dataset.s; renderSlate();
});
document.getElementById('modes').addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  document.querySelectorAll('#modes button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); deskMode=b.dataset.mode; renderSlate();
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
