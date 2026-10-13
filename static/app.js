'use strict';
const $ = id => document.getElementById(id);
const fmt = value => Number(value).toLocaleString('ko-KR');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const shortRoom = room => room.replace('[채권] ', '');
let rows = [], snapshot = null, page = 1, pages = 0, activeParams = null, requestId = 0, abort = null;
let statusRunning = false, statusTimer = null, lastRoomMarkup = '', latestVersion = 0;

function paramsFromForm() {
  const params = new URLSearchParams();
  for (const [key, id] of [['q','query'],['scope','scope'],['room','room'],['side','side'],['from','from'],['to','to']]) params.set(key, $(id).value.trim());
  return params;
}

async function search(fresh = true, nextPage = 1) {
  const mine = ++requestId;
  if (abort) abort.abort();
  abort = new AbortController();
  const params = fresh || !activeParams ? paramsFromForm() : new URLSearchParams(activeParams);
  if (!fresh && snapshot !== null) params.set('snapshot', snapshot);
  params.set('page', nextPage);
  $('searchButton').disabled = true; $('searchButton').textContent = '조회 중'; $('error').hidden = true;
  $('prev').disabled = $('next').disabled = true;
  try {
    const response = await fetch('/api/search?' + params, {signal:abort.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '조회에 실패했습니다.');
    if (mine !== requestId) return;
    rows = data.rows; page = data.page; pages = data.pages; snapshot = data.snapshot;
    activeParams = new URLSearchParams(params); activeParams.delete('page'); activeParams.delete('snapshot');
    $('matches').textContent = fmt(data.total) + '건';
    if (fresh) $('asOf').textContent = data.as_of;
    $('elapsed').textContent = fmt(data.elapsed_ms) + ' ms';
    $('answer').textContent = `${params.get('q') ? '“' + params.get('q') + '” 조건으로 ' : '조회 시점까지 '} ${fmt(data.total)}건을 찾았습니다.`;
    const f = data.filters;
    const labels = [f.terms.join(' + '), f.room && shortRoom(f.room), f.side, f.date_from && f.date_from+'부터', f.date_to && f.date_to+'까지'].filter(Boolean);
    $('applied').textContent = `적용 조건: ${labels.join(' · ') || '전체 내역'} / ${{bond:'종목·대화 내용',sender:'송신자',all:'전체 항목'}[f.scope]} 검색`;
    $('rows').innerHTML = rows.map((r, i) => `<tr><td class="time">${esc(r.timestamp)}${r.source === 'legacy_csv' ? '<br>기존 CSV' : ''}</td><td class="room-name">${esc(shortRoom(r.room))}</td><td class="ticker">${esc(r.ticker || '미분류')}</td><td>${esc(r.maturity || '—')}</td><td><span class="badge ${r.side.startsWith('매수')?'buy':r.side.startsWith('매도')?'sell':r.side.startsWith('거래')?'trade':''}">${esc(r.side || '—')}</span></td><td>${esc(r.sender || '—')}</td><td class="body-text">${esc(r.content)}</td><td><button class="copy" data-copy="${i}" aria-label="${i+1}번 행 복사">복사</button></td></tr>`).join('');
    $('empty').hidden = rows.length > 0;
    $('pageCount').textContent = `${pages ? page : 0} / ${fmt(pages)} 페이지`;
    $('pageNote').textContent = `최신 저장 순 · ${fmt(data.total)}건 중 ${rows.length ? fmt((page-1)*100+1) : 0}–${fmt((page-1)*100+rows.length)}건`;
    updateRefresh();
  } catch (err) {
    if (err.name !== 'AbortError' && mine === requestId) { $('error').textContent = err.message; $('error').hidden = false; }
  } finally {
    if (mine === requestId) {
      $('searchButton').disabled = false; $('searchButton').textContent = '조회 ↗';
      $('prev').disabled = page <= 1; $('next').disabled = page >= pages;
    }
  }
}

function updateRefresh() {
  const changed = snapshot !== null && latestVersion > snapshot;
  $('refresh').classList.toggle('new', changed);
  $('refresh').textContent = changed ? '새 내역 있음 · 다시 조회' : '다시 조회';
}

async function status() {
  clearTimeout(statusTimer);
  if (document.hidden || statusRunning) return;
  statusRunning = true;
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error();
    const data = await response.json();
    latestVersion = data.version; $('total').textContent = fmt(data.total)+'건';
    const connected = data.rooms.filter(r => r.status === 'connected').length;
    $('serverStatus').textContent = `서버 연결됨 · ${connected}/2방 수집`;
    const labels = {connected:'연결됨',offline:'미연결',retrying:'재연결 중',waiting:'연결 대기',disabled:'조회 전용'};
    const markup = data.rooms.map(r=>`<div class="room-card"><span class="dot ${esc(r.status)}"></span><div><strong>${esc(shortRoom(r.room))} <span class="room-name">· ${labels[r.status] || '확인 중'}</span></strong><p>${esc(r.detail)}</p></div><span class="interval">${r.interval ? r.interval+'초 간격' : '수집 꺼짐'}</span></div>`).join('');
    if (markup !== lastRoomMarkup) { $('rooms').innerHTML = markup; lastRoomMarkup = markup; }
    const errors = data.imports.errors || [];
    $('engineNote').textContent = errors.length ? 'CSV 가져오기 안내: '+errors.join(' / ') : '연결되지 않은 방은 자동 재시도합니다.';
    updateRefresh();
  } catch (_) { $('serverStatus').textContent = '서버 재연결 중'; }
  finally { statusRunning = false; if (!document.hidden) statusTimer = setTimeout(status, 5000); }
}

$('searchForm').addEventListener('submit', event=>{event.preventDefault(); search(true);});
$('refresh').addEventListener('click', ()=>search(true));
$('prev').addEventListener('click', ()=>search(false,page-1));
$('next').addEventListener('click', ()=>search(false,page+1));
$('resetFilters').addEventListener('click', ()=>{ $('searchForm').reset(); search(true); });
document.querySelectorAll('[data-query]').forEach(button=>button.addEventListener('click', ()=>{ $('searchForm').reset(); $('query').value=button.dataset.query; search(true); }));
$('rows').addEventListener('click', async event=>{
  const button = event.target.closest('[data-copy]'); if (!button) return;
  const r = rows[Number(button.dataset.copy)];
  try { await navigator.clipboard.writeText([r.timestamp,r.room,r.sender,r.ticker,r.maturity,r.side,r.content].join(' | ')); button.textContent='완료'; }
  catch (_) { button.textContent='실패'; }
  setTimeout(()=>button.textContent='복사', 1500);
});
document.addEventListener('visibilitychange', ()=>{ if (document.hidden) clearTimeout(statusTimer); else status(); });
search(true); status();
