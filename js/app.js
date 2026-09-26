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
      sports: data.summary?.sports || data.summary?.by_sport && Object.keys(data.summary.by_sport) || ['MLB','NFL','CFB'],
      props_total: data.summary?.props_total ?? props.length,
      props_clear: data.summary?.props_clear ?? props.filter(p=>p.tag==='CLEAR').length,
    },
    clears, fills, holds, props, parlays, teasers, rules,
    ledger: data.ledger || null,
    nfl_props: data.nfl_props || { games: [] },
    nfl_props_meta: data.nfl_props_meta || null,
    banner: data.banner || '',
    source: data.source || '',
    practice_run: data.practice_run===true,
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
const DESK_UI_VERSION='0925-practice';
function fmtPct(v){
  if(v==null || v==='') return '—';
  const n=Number(v);
  return Number.isFinite(n) ? `${Math.round(n*10)/10}%` : '—';
}
function fmtNum(v, {signed=false, dp=null}={}){
  if(v==null || v==='') return '—';
  const n=Number(v);
  if(!Number.isFinite(n)) return String(v);
  const r = dp!=null ? n.toFixed(dp) : String(n);
  return signed && n>0 ? `+${r}` : r;
}
function fmtPrice(v){
  if(v==null || v==='') return '';
  const n=Number(v);
  return Number.isFinite(n) ? (n>0?`+${n}`:String(n)) : String(v);
}
// O/U row: OVER x% | UNDER y% | TOTAL — both sides, higher side highlighted.
function ouRow(p){
  const ov=p.model_over_pct!=null && p.model_over_pct!=='' ? Number(p.model_over_pct) : null;
  const un=p.model_under_pct!=null && p.model_under_pct!=='' ? Number(p.model_under_pct) : null;
  const mOv=p.market_over_pct, mUn=p.market_under_pct;
  // No model O/U filed → show MARKET no-vig O/U big (labeled MKT), never as a model lean.
  if(ov==null && un==null && ((mOv!=null && mOv!=='') || (mUn!=null && mUn!==''))){
    const g=el('div','metrics ou mkt-only');
    const c=(k,v,sub)=>{const d=el('div'); d.append(el('div','k',k), el('div','v serif',v)); if(sub) d.append(el('div','sub',sub)); return d;};
    const sb=(price,book)=>[price!=null&&price!==''?fmtPrice(price):'', book||''].filter(Boolean).join(' ');
    g.append(
      c('MKT OVER', fmtPct(mOv), sb(p.over_price, p.over_book)),
      c('MKT UNDER', fmtPct(mUn), sb(p.under_price, p.under_book)),
      c('TOTAL', p.total_line!=null && p.total_line!=='' ? String(p.total_line) : '—', p.totals_source||''),
    );
    return g;
  }
  const hiSide = (ov!=null && un!=null && ov!==un) ? (ov>un?'over':'under') : null;
  const g=el('div','metrics ou');
  const cell=(k, v, sub, hi)=>{
    const d=el('div', hi?'hi':null);
    d.append(el('div','k',k), el('div','v serif',v));
    if(sub) d.append(el('div','sub',sub));
    return d;
  };
  const sub=(price, mkt)=>[price?`${fmtPrice(price)}`:'', mkt!=null&&mkt!==''?`mkt ${fmtPct(mkt)}`:''].filter(Boolean).join(' · ');
  g.append(
    cell('OVER', fmtPct(ov), sub(p.over_price, mOv), hiSide==='over'),
    cell('UNDER', fmtPct(un), sub(p.under_price, mUn), hiSide==='under'),
    cell('TOTAL', p.total_line!=null && p.total_line!=='' ? String(p.total_line) : '—', p.totals_source||'', false),
  );
  return g;
}
// Compact Model line: model · market (no-vig) · shrunk · edge · fair · source. Blank → —.
function modelLine(p){
  const d=el('div','mline');
  const bits=[];
  const add=(k,v)=>bits.push([k,v]);
  add('Model', fmtPct(p.model_win_pct));
  add('Mkt', fmtPct(p.market_win_pct));
  add('No-vig', fmtPct(p.market_novig_pct));
  add('Shrunk', fmtPct(p.shrunk_pct));
  if(p.shrink_w!=null && p.shrink_w!=='') add('w', fmtNum(p.shrink_w));
  const ePct=p.edge_pct, ePts=p.edge_pts;
  const eStr=[ePct!=null&&ePct!==''?`${fmtNum(ePct,{signed:true})}pp`:'', ePts!=null&&ePts!==''?`${fmtNum(ePts)} pts`:''].filter(Boolean).join(' · ') || '—';
  add('Edge', eStr);
  const fair = p.fair_line!=null&&p.fair_line!=='' ? fmtNum(p.fair_line,{signed:true}) : (p.fair_spread!=null&&p.fair_spread!=='' ? fmtNum(p.fair_spread,{signed:true}) : '—');
  add('Fair', fair);
  add('Sim', p.sim_flag||'—');
  add('Src', p.model_source||p.sim_source||'—');
  if(p.gap_flag) add('Gap', p.gap_flag);
  bits.forEach(([k,v])=>{
    const s=el('span', v==='—'?'blank':null);
    s.append(el('b',null,k+' '), document.createTextNode(v));
    d.append(s);
  });
  return d;
}
// Spread + ML lines with MARKET no-vig % (auto board rows). Skipped when absent.
function linesBlock(p){
  const has=(v)=>v!=null && v!=='';
  if(!has(p.spread_home_line) && !has(p.ml_home_price)) return null;
  const d=el('div','mline lines');
  const ab=(n)=>String(n||'').split(' ').slice(-1)[0];
  const add=(k,txt)=>{const s=el('span'); s.append(el('b',null,k+' '), document.createTextNode(txt)); d.append(s);};
  if(has(p.spread_home_line)){
    const sgn=(x)=>Number(x)>0?`+${x}`:String(x);
    add('Spread', `${ab(p.away)} ${sgn(p.spread_away_line)} ${fmtPrice(p.spread_away_price)} ${p.spread_away_book||''} · mkt ${fmtPct(p.market_away_cover_pct)}`);
    add('', `${ab(p.home)} ${sgn(p.spread_home_line)} ${fmtPrice(p.spread_home_price)} ${p.spread_home_book||''} · mkt ${fmtPct(p.market_home_cover_pct)}`);
  }
  if(has(p.ml_home_price)){
    add('ML', `${ab(p.away)} ${fmtPrice(p.ml_away_price)} (${fmtPct(p.market_away_ml_pct)}) · ${ab(p.home)} ${fmtPrice(p.ml_home_price)} (${fmtPct(p.market_home_ml_pct)})`);
  }
  return d;
}
function playCard(p, {star}={}){
  const a=el('article','play'+(star||p.tag==='CLEAR'?' clear-play':''));
  a.dataset.sport=p.sport||'OTHER';
  a.dataset.kind=p.kind||'side';
  const hd=el('div','play-hd');
  const pill=el('span','pill'+(star?' star':''), star?'PLAY':(p.tag||'CLEAR'));
  hd.append(pill, el('span','when',[p.sport||'', kickInfo(p).short, `${p.units||0}u`].filter(Boolean).join(' · ')));
  a.append(hd);
  a.append(el('h3','serif',p.matchup||`${p.away||''} at ${p.home||''}`));
  const noSide = p.board_only ? 'Board — no Main side' : (p.tag==='HOLD'||p.tag==='BOARD' ? 'Hold — no number' : '');
  a.append(el('div','side',p.selection|| noSide));
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
  a.append(ouRow(p));
  const lb=linesBlock(p); if(lb) a.append(lb);
  a.append(modelLine(p));
  const shop=[p.book, p.price_american!=null?fmtPrice(p.price_american):''].filter(Boolean).join(' ');
  if(shop) a.append(el('p','shop',shop));
  if(p.price_condition){
    const pc=el('p','price-cond');
    pc.append(el('b',null,'Price: '), document.createTextNode(p.price_condition));
    a.append(pc);
  }
  const w=el('p', 'why why-line'+(p.why?'':' muted'));
  w.append(el('b',null,'Why: '), document.createTextNode(p.why || 'no note filed'));
  a.append(w);
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

function ledgerSettledCard(r){
  const res=(r.result||'').toUpperCase() || '—';
  const a=el('article','play'+(res==='W'?' clear-play':''));
  a.dataset.sport=r.sport||'';
  const hd=el('div','play-hd');
  const pill=el('span','pill'+(res==='W'?' star':''), res);
  const when=[r.sport||'', r.units!=null?`${r.units}u`:''].filter(Boolean).join(' · ');
  hd.append(pill, el('span','when',when));
  a.append(hd);
  a.append(el('h3','serif',r.selection||r.lean||r.id||'—'));
  const m=el('div','metrics');
  const pnl=r.pnl_units;
  let pnlStr='—';
  if(pnl!=null && pnl!==''){
    const n=Number(pnl);
    pnlStr=(n>=0?'+':'')+String(Math.round(n*100)/100)+'u';
  }
  for(const [k,v] of [['SCORE', r.score||'—'],['PnL', pnlStr],['SLATE', r.slate||r.slate_date_ct||'—']]){
    const d=el('div');
    d.append(el('div','k',k), el('div','v serif',String(v)));
    m.append(d);
  }
  a.append(m);
  if(r.notes || r.source) a.append(el('p','why',[r.source, r.notes].filter(Boolean).join(' · ')));
  return a;
}
function ledgerOpenCard(r){
  const a=el('article','play');
  a.dataset.sport=r.sport||'';
  const hd=el('div','play-hd');
  hd.append(el('span','pill','OPEN'), el('span','when',[r.sport||'', r.units!=null?`${r.units}u`:''].filter(Boolean).join(' · ')));
  a.append(hd);
  a.append(el('h3','serif',r.lean||r.selection||r.id||'—'));
  const m=el('div','metrics');
  for(const [k,v] of [['SLATE', r.slate||'—'],['BOOK', r.book||'—'],['PRICE', r.price||(r.price_american!=null?String(r.price_american):'—')]]){
    const d=el('div');
    d.append(el('div','k',k), el('div','v serif',String(v)));
    m.append(d);
  }
  a.append(m);
  if(r.notes) a.append(el('p','why',r.notes));
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

function nflPropCard(p, rank){
  const a=el('article','play');
  const hd=el('div','play-hd');
  hd.append(el('span','pill',`#${rank}`), el('span','when',[p.market||'PROP', p.book||''].filter(Boolean).join(' · ')));
  a.append(hd);
  a.append(el('h3','serif',p.player||p.selection||'Prop'));
  a.append(el('div','side',p.selection||`${p.side||''} ${p.line!=null?p.line:''}`.trim()));
  const m=el('div','metrics');
  const line=p.line!=null?String(p.line):'—';
  const price=p.price_american!=null?String(p.price_american):'—';
  const pairs=[['LINE', line],['PRICE', price]];
  if(p.model_win_pct!=null) pairs.push(['MODEL', `${p.model_win_pct}%`]);
  const edge=p.edge_pct ?? p.edge_ev ?? p.edge;
  if(edge!=null && edge!=='') pairs.push(['EDGE', String(edge)]);
  for(const [k,v] of pairs){
    const d=el('div');
    d.append(el('div','k',k), el('div','v serif',v));
    m.append(d);
  }
  a.append(m);
  if(p.notes) a.append(el('p','why',p.notes));
  return a;
}
function renderNflProps(data){
  const bits=document.getElementById('nflPropsBits');
  if(!bits) return;
  bits.innerHTML='';
  const games=(data.nfl_props&&data.nfl_props.games)||[];
  const gEl=document.getElementById('propsGameCount');
  const tEl=document.getElementById('propsTotalCount');
  const total=games.reduce((n,g)=>n+((g.props||[]).length),0);
  if(gEl) gEl.textContent=String(games.length);
  if(tEl) tEl.textContent=String(total);
  if(!games.length){
    bits.append(el('p','why','No NFL props yet — waiting on Week 1 nfl_game_props feed. CFB props stay off DESK.'));
    return;
  }
  games.forEach(g=>{
    bits.append(el('p','section-label', g.matchup || g.event_id || 'NFL'));
    const props=(g.props||[]).slice(0,4);
    if(!props.length) bits.append(el('p','why','No props ranked for this game.'));
    else props.forEach((p,i)=>bits.append(nflPropCard(p, p.rank||i+1)));
  });
}
function showPanel(id){
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on', p.id==='p-'+id));
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on', b.dataset.p===id));
}
// ---- Start times (always shown in CT, America/Chicago) ----
const CT_TZ='America/Chicago';
const SPORT_ORDER=['MLB','CFB','NFL'];
const CT_FMT=new Intl.DateTimeFormat('en-US',{timeZone:CT_TZ,weekday:'short',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const WEEKDAYS=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
function kickFromParts(date, wd, h, mi){
  const h12=(h%12)||12, ap=h<12?'AM':'PM', mm=String(mi).padStart(2,'0');
  return {
    key:`${date} ${String(h).padStart(2,'0')}:${mm}`,       // CT wall clock, sortable
    short:`${wd} ${h12}:${mm}${ap==='AM'?'a':'p'} CT`,      // row label, e.g. "Sat 11:00a CT"
    long:`${wd} ${h12}:${mm} ${ap} CT`,                     // group header, e.g. "Sat 11:00 AM CT"
  };
}
// Real start time from the data only: kick_utc first, else the published kick_ct / kick_et
// wall-clock on the game's CT slate date. Nothing found -> TBD (never invented).
function kickInfo(p){
  if(p && p._kick) return p._kick;
  let info=null;
  const iso=p.kick_utc||p.commence_time||p.start_time_utc||'';
  if(iso){
    const d=new Date(iso);
    if(!isNaN(d.getTime())){
      const o={}; CT_FMT.formatToParts(d).forEach(x=>{o[x.type]=x.value;});
      info=kickFromParts(`${o.year}-${o.month}-${o.day}`, o.weekday, Number(o.hour)%24, Number(o.minute));
    }
  }
  if(!info){
    const date=p.slate_date_ct||(deskData&&deskData.slate_date)||'';
    const tryWall=(txt, zone, shift)=>{
      const m=new RegExp('^\\s*(\\d{1,2}):(\\d{2})\\s*([AP])\\.?M\\.?\\s*'+zone+'\\b','i').exec(txt||'');
      if(!m || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
      let h=(Number(m[1])%12)+(m[3].toUpperCase()==='P'?12:0)+shift;
      if(h<0||h>23) return null;
      return kickFromParts(date, WEEKDAYS[new Date(date+'T12:00:00Z').getUTCDay()], h, Number(m[2]));
    };
    info=tryWall(p.kick_ct,'C[DS]?T',0) || tryWall(p.kick_et,'E[DS]?T',-1);
  }
  if(!info) info={key:'', short:'TBD', long:'Time TBD'};
  try{ Object.defineProperty(p,'_kick',{value:info,enumerable:false}); }catch(e){}
  return info;
}
function sportRank(s){
  const i=SPORT_ORDER.indexOf(s||'');
  return i<0 ? SPORT_ORDER.length : i;
}
function byKickTime(a,b){
  const ak=kickInfo(a).key, bk=kickInfo(b).key;
  if(ak!==bk){
    if(!ak) return 1;          // TBD sinks to the bottom
    if(!bk) return -1;
    return ak<bk?-1:1;
  }
  const sr=sportRank(a.sport)-sportRank(b.sport);
  if(sr) return sr;
  const ss=String(a.sport||'').localeCompare(String(b.sport||''));
  if(ss) return ss;
  const am=a.matchup||`${a.away||''} at ${a.home||''}`, bm=b.matchup||`${b.away||''} at ${b.home||''}`;
  return String(am).localeCompare(String(bm)) || String(a.selection||'').localeCompare(String(b.selection||''));
}
// Rows are already sorted; drop a small time-slot header before each new slot,
// plus a jump bar of the slots at the top when there is more than one.
function renderTimeGroups(slate, rows, makeCard){
  const groups=[];
  rows.forEach(p=>{
    const k=kickInfo(p);
    const last=groups[groups.length-1];
    if(last && last.key===k.key) last.rows.push(p);
    else groups.push({key:k.key, long:k.long, short:k.short, rows:[p]});
  });
  if(groups.length>1){
    const bar=el('nav','jump');
    bar.setAttribute('aria-label','Jump to start time');
    groups.forEach((g,i)=>{
      const b=el('button',null,g.key?g.short.replace(/ CT$/,''):'TBD');
      b.type='button';
      b.dataset.slot=String(i);
      b.title=`${g.long} · ${g.rows.length}`;
      bar.append(b);
    });
    bar.addEventListener('click',e=>{
      const b=e.target.closest('button'); if(!b) return;
      const t=document.getElementById('slot-'+b.dataset.slot);
      if(t) t.scrollIntoView({behavior:'smooth',block:'start'});
    });
    slate.append(bar);
  }
  groups.forEach((g,i)=>{
    const h=el('p','section-label time-slot',`${g.long} · ${g.rows.length}`);
    h.id='slot-'+i;
    slate.append(h);
    g.rows.forEach(p=>slate.append(makeCard(p)));
  });
}
function sportMatch(p, s){
  return s==='ALL' || (p.sport||'')===s;
}
function updateSportChips(){
  if(!deskData) return;
  const all=[...deskData.clears, ...deskData.fills, ...deskData.holds];
  document.querySelectorAll('#chips button').forEach(b=>{
    const s=b.dataset.s;
    if(!s) return;
    const n=s==='ALL' ? all.length : all.filter(p=>sportMatch(p, s)).length;
    const label=s==='ALL' ? 'All' : s;
    b.title=`${s==='ALL'?'ALL':s} · ${n}`;
    b.setAttribute('aria-label', `${label}, ${n} games`);
    b.textContent=n>0 ? `${label} ${n}` : label;
  });
}
function renderSlate(){
  if(!deskData) return;
  const slate=document.getElementById('slate');
  const hint=document.getElementById('sportHint');
  slate.innerHTML='';
  let rows=[];
  updateSportChips();
  if(deskMode==='plays'){
    rows=deskData.clears.filter(p=>sportMatch(p, deskSport)).sort(byKickTime);
    if(hint) hint.textContent = deskSport==='ALL' ? 'Recommended · CLEAR only' : `${deskSport} plays · CLEAR only`;
    if(!rows.length){
      const boardN=(deskData.fills.length||0)+(deskData.holds.length||0);
      let emptyMsg='No CLEARs today — open Board to see the full slate.';
      if(deskData.clears.length===0 && boardN>0){
        emptyMsg='No CLEARs today — open Board to see the full slate.';
      }
      slate.append(el('p','why',emptyMsg));
    } else renderTimeGroups(slate, rows, p=>playCard(p,{star:true}));
  } else {
    // board (default fallback)
    deskMode='board';
    // Luis Sep 25: every game on the Board — CLEAR + FILL + HOLD/BOARD, no cap.
    rows=[...deskData.clears, ...deskData.fills, ...deskData.holds].filter(p=>sportMatch(p, deskSport)).sort(byKickTime);
    if(hint) hint.textContent = (deskSport==='ALL' ? 'Full board' : `${deskSport} board`) + ` · every game · ${rows.length}`;
    if(!rows.length) slate.append(el('p','why','No board games for this filter.'));
    else renderTimeGroups(slate, rows, p=>playCard(p));
  }
}
async function main(){
  deskData=await loadDesk();
  const data=deskData;
  document.getElementById('asof').textContent=`${data.slate_date}\n${data.timezone}\n${data.as_of_label||''}`;
  document.getElementById('clearCount').textContent=String(data.summary?.published_clear ?? data.clears?.length ?? 0);
  const bc=document.getElementById('boardCount');
  if(bc) bc.textContent=String((data.clears?.length||0)+(data.fills?.length||0)+(data.holds?.length||0));
  document.getElementById('asOfFoot').textContent=`As of ${data.as_of_label||''} · ${data.clears.length} plays`;

  const bn=document.getElementById('deskBanner');
  if(bn){ bn.textContent=data.banner||''; bn.hidden=!data.banner; bn.classList.toggle('practice', !!data.practice_run); }
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
    const openEl=document.getElementById('openClearCount');
    if(openEl) openEl.textContent=String((data.ledger.open||[]).length);
    const sh=data.ledger.shadow;
    const sb=document.getElementById('shadowBook');
    const shEl=document.getElementById('shadowHigh');
    if(sb) sb.textContent=(sh&&sh.written_book)||'—';
    if(shEl) shEl.textContent=(sh&&sh.high_model_book)||'—';
    const bits=document.getElementById('ledgerBits');
    if(bits){
      bits.innerHTML='';
      const settled=data.ledger.settled||[];
      const open=data.ledger.open||[];
      if(!settled.length && !open.length){
        bits.append(el('p','why','No graded CLEARs yet.'));
      } else {
        if(settled.length){
          bits.append(el('p','section-label','Settled'));
          settled.forEach(r=>bits.append(ledgerSettledCard(r)));
        }
        if(open.length){
          bits.append(el('p','section-label','Open'));
          open.forEach(r=>bits.append(ledgerOpenCard(r)));
        }
      }
      // Separate paper section — never mixed into Written CLEAR bankroll.
      if(sh){
        bits.append(el('p','section-label', sh.label||'Shadow (paper)'));
        const note=el('p','why', sh.note||'Paper 1u FILLs — NOT bankroll / NOT CLEARs');
        bits.append(note);
        const sSettled=sh.settled||[];
        const sOpen=sh.open||[];
        if(!sSettled.length && !sOpen.length){
          bits.append(el('p','why','No shadow tickets yet.'));
        } else {
          if(sSettled.length){
            bits.append(el('p','section-label','Shadow settled'));
            sSettled.forEach(r=>bits.append(ledgerSettledCard(r)));
          }
          if(sOpen.length){
            bits.append(el('p','section-label','Shadow open'));
            sOpen.forEach(r=>bits.append(ledgerOpenCard(r)));
          }
        }
      }
    }
  }
  renderNflProps(data);
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
