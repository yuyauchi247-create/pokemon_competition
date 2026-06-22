---
name: kaggle-top-logs
description: Kaggle「pokemon-tcg-ai-battle」リーダーボード上位N位の各プレイヤー(最高スコアエージェント)について、直近M試合の Observation(replay.json由来) を抽出し kaggle_top10_observations/ に保存する。「上位◯位のログ抽出」「リーダーボードの各プレイヤーの対戦ログを集める」等で使用。
---

# Kaggle 上位プレイヤーの対戦ログ抽出

Kaggle の `pokemon-tcg-ai-battle` リーダーボード上位 **N位**（既定10）の各チームについて、最高スコアエージェントの **直近M試合**（既定30）の Observation を抽出し `kaggle_top10_observations/` に保存する。各JSONは `replay.json` の `steps[n-2]`（最終ステップ直前）の能動側 observation から `search_begin_input` を除いたもの＝ビジュアライザ右側の Observation パネルと同一。

## パラメータ（args があれば反映、無ければ既定）
- 上位件数 N（既定 10）
- 1チームあたり試合数 M（既定 30）
- 出力先（既定 `kaggle_top10_observations/`）

## 前提・注意
- Playwright(MCP) で **Kaggle にログイン済み**であること。未ログインなら `https://www.kaggle.com/account/login` を開き、ユーザーにログインしてもらう（入力は人間が行う）。
- 取得は **ブラウザ内 fetch** で Kaggle の内部APIを叩く（cookie の `XSRF-TOKEN`/`build-hash` を使用）。`replay.json` は1試合 数MB あるため **429(レート制限)** が出る。並列を抑え、429 は指数バックオフで再試行する。
- env メタ(`specification` 等)は kaggle-environments 非同梱のため完全一致は不要。抽出するのは observation 本体（同じ cg シミュレータ由来で中身は一致）。

## 手順

### 1. リーダーボードを開く
`mcp__playwright__browser_navigate` で `https://www.kaggle.com/competitions/pokemon-tcg-ai-battle/leaderboard`。
（ログインを促されたら login ページを開き、人間にログインしてもらってから再度開く。）

### 2. 上位N位の submissionId を取得
`browser_evaluate`（`N` を埋め込む）。`live_tv` クリックで URL の `submissionId` が「そのチームの最高スコアエージェント」に更新される挙動を使う:
```js
async () => {
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const getSub=()=>{const m=location.href.match(/submissionId=(\d+)/);return m?m[1]:null;};
  const out=[]; const N=10;
  for(let i=0;i<N;i++){
    const spans=[...document.querySelectorAll('span[role="button"]')].filter(s=>(s.textContent||'').trim()==='live_tv');
    let row=spans[i]; for(let k=0;k<10&&row;k++){ if(/\d{3,4}\.\d/.test(row.textContent||''))break; row=row.parentElement;}
    const txt=(row?.textContent||'').trim(); const mm=txt.match(/^(\d+)(.+?)(\d{3,4}\.\d)/);
    const before=getSub(); spans[i].click();
    for(let t=0;t<60;t++){ await sleep(100); const cur=getSub(); if(cur&&cur!==before)break; }
    await sleep(200);
    out.push({rank:mm?+mm[1]:i+1, team:mm?mm[2].trim():'', submissionId:getSub()});
  }
  return out;
}
```

### 3. ヘルパーとチーム処理関数を定義
`browser_evaluate` で window に定義（`__TEAMS` は手順2の結果、`M`=試合数を埋め込む）:
```js
() => {
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  window.__TEAMS=/* 手順2の配列 [{rank,team,submissionId}] */;
  const gc=n=>{const m=document.cookie.match(new RegExp('(?:^|;\\s*)'+n+'=([^;]+)'));return m?decodeURIComponent(m[1]):null;};
  window.__xsrf=gc('XSRF-TOKEN'); window.__build=gc('build-hash')||'';
  window.__listEpisodes=async(submissionId)=>{
    const res=await fetch('/api/i/competitions.EpisodeService/ListEpisodes',{method:'POST',credentials:'include',
      headers:{'content-type':'application/json','accept':'application/json','x-xsrf-token':window.__xsrf,'x-kaggle-build-version':window.__build},
      body:JSON.stringify({ids:[],submissionId,successfulOnly:true,includeInProgress:false})});
    if(!res.ok) throw new Error('listEpisodes '+res.status); return res.json();
  };
  window.__processTeam=async(idx,M)=>{
    const t=window.__TEAMS[idx];
    const d=await window.__listEpisodes(t.submissionId);
    let eps=(d.episodes||[]).filter(e=>e.state==='COMPLETED');
    eps.sort((a,b)=>new Date(b.endTime)-new Date(a.endTime)); eps=eps.slice(0,M||30);
    const games=new Array(eps.length);
    const fetchReplay=async(id)=>{
      for(let a=0;a<8;a++){ const res=await fetch(`/competitions/episodes/${id}/replay.json`,{credentials:'include'});
        if(res.status===429){await sleep(2500*(a+1));continue;} if(!res.ok)throw new Error('replay '+res.status); return res.json(); }
      throw new Error('replay 429 (gave up)');
    };
    let next=0;
    async function worker(){ while(next<eps.length){ const k=next++; const e=eps[k];
      try{ const rep=await fetchReplay(e.id); const steps=rep.steps; const n=steps.length; const ti=n-2; const step=steps[ti];
        let ag=step.find(x=>x.status==='ACTIVE'||(x.observation&&x.observation.step===ti))||step[0];
        const obs=Object.assign({},ag.observation); delete obs.search_begin_input;
        const my=(e.agents||[]).find(g=>g.submissionId===t.submissionId)||e.agents[0];
        const op=(e.agents||[]).find(g=>g.submissionId!==t.submissionId);
        games[k]={episodeId:e.id,endTime:e.endTime,result:my?(my.reward>0?'W':(my.reward<0?'L':'D')):'?',
                  opponentSubmissionId:op?op.submissionId:null,numSteps:n,targetStep:ti,observation:obs};
      }catch(err){ games[k]={episodeId:e.id,error:String(err)}; } await sleep(150); } }
    await Promise.all([0,1].map(()=>worker()));   // 並列2（429回避）
    return {rank:t.rank,team:t.team,submissionId:t.submissionId,
            totalEpisodes:(d.episodes||[]).length,taken:games.length,
            errors:games.filter(g=>g.error).length,games};
  };
  return 'defined';
}
```

### 4. 各チームを処理してファイル保存
チームごとに `browser_evaluate` を `filename:"teamNN_<team>.json"` 付きで呼ぶ（戻り値がそのファイルに保存される）:
```js
async () => { return await window.__processTeam(IDX, M); }
```
- `errors>0` のチームは、その episodeId だけを並列1・長めバックオフで再取得してマージする（patchTeam）。

### 5. フォルダへ整理（Python）
保存した `teamNN_*.json` を本スキル同梱の `organize.py` で整理:
```
python3 .claude/skills/kaggle-top-logs/organize.py 'team*.json' kaggle_top10_observations
```
出力: `kaggle_top10_observations/rankNN_<team>/NN_ep<id>_<W/L>.json`（新しい順）＋ 各チーム `_index.json` ＋ `manifest.json`。

### 6. 後片付け
作業用の `team*.json`・`.playwright-mcp/` 等の一時物は削除する。`kaggle_top10_observations/` を成果物として残す。

## 検証ポイント
- 各チーム `taken==M`・`errors==0`。
- 各 observation のキーが `current/logs/remainingOverageTime/select/step`（`search_begin_input` を除く）。
- 1件をビジュアライザの当該ステップと突き合わせると一致する。
