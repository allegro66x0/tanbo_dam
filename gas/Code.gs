/**
 * 田んぼダム 水位計測 — 受信用 GAS
 *
 * 書き込み先: スプレッドシート「tambo_data2」(SPREADSHEET_ID)。
 * tambo_data2 にバインドしても、単体の Apps Script プロジェクトでも動く。
 * 別のファイルに切り替えるときはスクリプトプロパティ SPREADSHEET_ID で上書きする。
 *
 * 親機(tanbo-parent v2)から JSON を POST で受け取り、月別シートに書き込む。
 *
 *   POST body: { token, parent_id, db_uuid, fw, local_node, kind: "m" | "h", rows: [ {id, ...}, ... ] }
 *   response : { ok: true, hwm: <受理済み最大 id>, added: <今回追加した行数>,
 *                assign: { "<node>": "<parent_id>", ... } }
 *
 * 重複排除: (db_uuid, kind) ごとに受理済みの最大 id(高水位線)をスクリプトプロパティに持ち、
 *           それ以下の id は捨てる。親機は返ってきた hwm までを送信済みにする。
 *
 * 分担表(親機が 2 台以上のとき): 「分担」シートの「ノード → 担当親機」を、すべての応答で返す。
 *   親機は自分の担当のノードだけを測る。GAS が返すのは分担表だけで、親機への命令は送らない。
 *   checkStale() は、ハートビートが FAILOVER_MIN 分(既定 30)途絶えた親機の担当(その親機の地点のノードを除く)を
 *   動いている親機へ移し、メールで知らせる。止まっていた親機が戻っても自動では戻さない(シートを手で書き換える)。
 *
 * 初回: エディタで setup() を1回実行 → ログに出る TOKEN を親機の設定に書く → ウェブアプリとしてデプロイ
 *       (実行ユーザー: 自分、アクセス: 全員)
 */

const VERSION = '2.2.0';
const SPREADSHEET_ID = '1UDnr3DmX6d8fwvJzFMTeNSNxzkm9DyW7RCXji2xLfSQ';   // tambo_data2
const TZ = 'Asia/Tokyo';
const SHEET_LATEST = '最新';
const SHEET_PARENT = '親機';
const SHEET_ASSIGN = '分担';
const SHEET_HELP = '説明';
const DEFAULT_STALE_MIN = 40;      // 親機からのハートビートがこれ以上途絶えたらメール
const DEFAULT_FAILOVER_MIN = 30;   // これ以上途絶えた親機の担当を、動いている親機へ移す
const DEFAULT_PARENT = 'parent-1';

// 分担シートが無いときの初期値(setup で作る)。親機の地点のノードは、その親機に固定
const INITIAL_ASSIGN = [
  [2, 'parent-1', '初期値'],
  [4, 'parent-1', '初期値(par2 に置き換える予定)'],
  [10, 'parent-2', '親機の地点(固定)'],
  [11, 'parent-1', '親機の地点(固定)'],
];

// 列は末尾にだけ足す(既存の月別シートの列位置を変えない)
const MEAS_HEADER = [
  'Timestamp', 'CycleTime', 'Node', 'Status', 'Dist20_cm', 'EchoMed_us', 'EchoMin_us',
  'EchoMax_us', 'Valid', 'Tries', 'Seq', 'TxStatus', 'RTT_ms', 'ChildUptime_s', 'FW',
  'ClockSynced', 'ParentRowId', 'Raw', 'Parent',
];
const HEALTH_HEADER = [
  'Timestamp', 'Interval_s', 'Cycle_ms', 'OK', 'Nodes', 'Queue_m', 'Queue_h', 'Batt_V',
  'ModemSig_pct', 'XBeeAI', 'CPU_C', 'Load1', 'MemAvail_MB', 'DiskFree_MB', 'Uptime_h',
  'ClockSynced', 'UploadErr', 'FW', 'BootId', 'ParentRowId', 'Parent',
];
const LATEST_HEADER = [
  'Node', '最終受信', 'Status', 'Dist20_cm', 'EchoMed_us', 'Valid/Tries', '最終OK時刻',
  '最終OK距離_cm', '連続失敗', 'FW', '親機',
];
const ASSIGN_HEADER = ['Node', '担当親機', '変更日時', '理由'];
const OK_STATUSES = { OK: true, OK_V1: true };

// ------------------------------------------------------------------ entry points

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, error: 'invalid JSON' });
  }
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('TOKEN');
  if (!token || body.token !== token) {
    return json_({ ok: false, error: 'bad token' });
  }
  if (!body.db_uuid || !/^[0-9a-f]{8,64}$/.test(String(body.db_uuid))) {
    return json_({ ok: false, error: 'bad db_uuid' });
  }
  if (body.kind !== 'm' && body.kind !== 'h') {
    return json_({ ok: false, error: 'bad kind' });
  }
  if (!Array.isArray(body.rows)) {
    return json_({ ok: false, error: 'rows must be an array' });
  }
  const pid = String(body.parent_id || DEFAULT_PARENT);
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(pid)) {
    return json_({ ok: false, error: 'bad parent_id' });
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    return json_({ ok: false, error: 'lock timeout' });
  }
  try {
    const key = 'hwm_' + body.db_uuid + '_' + body.kind;
    let hwm = Number(props.getProperty(key) || 0);
    const rows = body.rows
      .filter(function (r) { return r && Number.isInteger(r.id) && r.id > hwm; })
      .sort(function (a, b) { return a.id - b.id; });

    const ss = ss_();
    if (rows.length) {
      if (body.kind === 'm') {
        writeMonthly_(ss, 'data_', MEAS_HEADER, rows, function (r) { return measRow_(r, pid); });
        updateLatest_(ss, rows, pid);
      } else {
        writeMonthly_(ss, 'health_', HEALTH_HEADER, rows, function (r) { return healthRow_(r, pid); });
        updateParent_(ss, rows[rows.length - 1], body, pid);
        props.setProperty('hb_' + pid, String(rows[rows.length - 1].ts));
      }
      hwm = rows[rows.length - 1].id;
      props.setProperty(key, String(hwm));
    }
    if (Number.isInteger(body.local_node)) {
      props.setProperty('local_' + pid, String(body.local_node));
    }
    props.setProperty('last_post_ts', String(Date.now() / 1000));
    return json_({ ok: true, hwm: hwm, added: rows.length, assign: readAssign_(ss) });
  } catch (err) {
    console.error(err && err.stack || err);
    return json_({ ok: false, error: String(err && err.message || err) });
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  return json_({ ok: true, version: VERSION });
}

// ------------------------------------------------------------------ writers

function ss_() {
  const id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID') || SPREADSHEET_ID;
  return SpreadsheetApp.openById(id);
}

function toDate_(ts) {
  return (ts === null || ts === undefined) ? '' : new Date(ts * 1000);
}

function v_(x) {
  return (x === null || x === undefined) ? '' : x;
}

function measRow_(r, pid) {
  return [
    toDate_(r.ts), toDate_(r.cycle_ts), r.node, r.status, v_(r.dist_cm), v_(r.med_us),
    v_(r.min_us), v_(r.max_us), v_(r.n_ok), v_(r.n_try), v_(r.seq), v_(r.tx_status),
    v_(r.rtt_ms), v_(r.child_uptime_s), v_(r.fw), v_(r.synced), r.id, v_(r.raw), pid,
  ];
}

function healthRow_(r, pid) {
  return [
    toDate_(r.ts), v_(r.interval_s), v_(r.cycle_ms), v_(r.n_ok), v_(r.n_nodes), v_(r.queue_m),
    v_(r.queue_h), v_(r.batt_v), v_(r.modem_sig), v_(r.xbee_ai), v_(r.cpu_temp), v_(r.load1),
    v_(r.mem_avail_mb), v_(r.disk_free_mb),
    (r.uptime_s === null || r.uptime_s === undefined) ? '' : Math.round(r.uptime_s / 36) / 100,
    v_(r.synced), v_(r.upload_err), v_(r.fw), v_(r.boot_id), r.id, pid,
  ];
}

/** 行を記録時刻(JST)の年月ごとのシートへまとめて書く。 */
function writeMonthly_(ss, prefix, header, rows, mapper) {
  const groups = {};
  const order = [];
  rows.forEach(function (r) {
    const month = Utilities.formatDate(new Date(r.ts * 1000), TZ, 'yyyy-MM');
    if (!groups[month]) {
      groups[month] = [];
      order.push(month);
    }
    groups[month].push(mapper(r));
  });
  order.forEach(function (month) {
    const sh = getOrCreateSheet_(ss, prefix + month, header);
    appendValues_(sh, groups[month]);
  });
}

function getOrCreateSheet_(ss, name, header) {
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight('bold');
    sh.setFrozenRows(1);
    sh.getRange('A:B').setNumberFormat('yyyy-mm-dd hh:mm:ss');
  } else if (sh.getRange(1, header.length).getValues()[0][0] === '') {
    // 列を末尾に足した版の前に作ったシート: 見出しだけ足す(既存の行はそのまま)
    sh.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight('bold');
  }
  return sh;
}

function appendValues_(sh, values) {
  if (!values.length) return;
  const start = sh.getLastRow() + 1;
  const need = start + values.length - 1 - sh.getMaxRows();
  if (need > 0) {
    sh.insertRowsAfter(sh.getMaxRows(), need + 500);
  }
  sh.getRange(start, 1, values.length, values[0].length).setValues(values);
}

/** 「最新」シート: ノードごとに1行。最新状態と最後に OK だった時刻・距離、連続失敗回数。 */
function updateLatest_(ss, rows, pid) {
  const sh = getOrCreateSheet_(ss, SHEET_LATEST, LATEST_HEADER);
  const n = Math.max(sh.getLastRow() - 1, 0);
  const cur = n ? sh.getRange(2, 1, n, LATEST_HEADER.length).getValues() : [];
  const byNode = {};
  cur.forEach(function (row) { if (row[0] !== '') byNode[row[0]] = row; });

  rows.forEach(function (r) {
    const prev = byNode[r.node] || [r.node, '', '', '', '', '', '', '', 0, '', ''];
    const ok = OK_STATUSES[r.status] === true;
    byNode[r.node] = [
      r.node, toDate_(r.ts), r.status, v_(r.dist_cm), v_(r.med_us),
      (r.n_try === null || r.n_try === undefined) ? '' : r.n_ok + '/' + r.n_try,
      ok ? toDate_(r.ts) : prev[6],
      ok ? v_(r.dist_cm) : prev[7],
      ok ? 0 : (Number(prev[8]) || 0) + 1,
      v_(r.fw) || prev[9],
      pid,
    ];
  });

  const out = Object.keys(byNode)
    .map(function (k) { return byNode[k]; })
    .sort(function (a, b) { return Number(a[0]) - Number(b[0]); });
  if (out.length) {
    if (out.length + 1 > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), out.length);
    sh.getRange(2, 1, out.length, LATEST_HEADER.length).setValues(out);
    sh.getRange(2, 2, out.length, 1).setNumberFormat('yyyy-mm-dd hh:mm:ss');
    sh.getRange(2, 7, out.length, 1).setNumberFormat('yyyy-mm-dd hh:mm:ss');
  }
}

/** 「親機」シート: 親機ごとに 1 列。最新のハートビートを縦に並べる。 */
function updateParent_(ss, h, body, pid) {
  const sh = getOrCreateSheet_(ss, SHEET_PARENT, ['項目', DEFAULT_PARENT]);
  const width = Math.max(sh.getLastColumn(), 2);
  const head = sh.getRange(1, 1, 1, width).getValues()[0];
  if (head[1] === '値') {   // 親機 1 台の版で作ったシート: その列は parent-1 のもの
    head[1] = DEFAULT_PARENT;
    sh.getRange(1, 2).setValues([[DEFAULT_PARENT]]);
  }
  let col = head.indexOf(pid) + 1;
  if (col < 1) {
    col = head.filter(function (x) { return x !== ''; }).length + 1;
    sh.getRange(1, col).setValues([[pid]]).setFontWeight('bold');
  }
  const kv = [
    ['最終ハートビート', toDate_(h.ts)],
    ['親機ID', v_(body.parent_id)],
    ['親機FW', v_(body.fw)],
    ['周期_s', v_(h.interval_s)],
    ['前回サイクル OK/全体', v_(h.n_ok) + '/' + v_(h.n_nodes)],
    ['未送信(計測)', v_(h.queue_m)],
    ['バッテリー_V', v_(h.batt_v)],
    ['電波品質_%', v_(h.modem_sig)],
    ['XBee AI', v_(h.xbee_ai)],
    ['CPU温度_C', v_(h.cpu_temp)],
    ['ディスク空き_MB', v_(h.disk_free_mb)],
    ['稼働時間_h', (h.uptime_s === null || h.uptime_s === undefined) ? '' : Math.round(h.uptime_s / 36) / 100],
    ['時刻同期', v_(h.synced)],
    ['送信エラー', v_(h.upload_err)],
  ];
  sh.getRange(2, 1, kv.length, 1).setValues(kv.map(function (x) { return [x[0]]; }));
  sh.getRange(2, col, kv.length, 1).setValues(kv.map(function (x) { return [x[1]]; }));
  sh.getRange(2, col).setNumberFormat('yyyy-mm-dd hh:mm:ss');
}

// ------------------------------------------------------------------ assignment

/** 「分担」シートを {node(文字列): parent_id} で返す。担当が空の行は無視する。 */
function readAssign_(ss) {
  const sh = ss.getSheetByName(SHEET_ASSIGN);
  const out = {};
  if (!sh || sh.getLastRow() < 2) return out;
  sh.getRange(2, 1, sh.getLastRow() - 1, 2).getValues().forEach(function (row) {
    const node = Number(row[0]);
    const pid = String(row[1]).trim();
    if (row[0] !== '' && Number.isInteger(node) && pid) out[String(node)] = pid;
  });
  return out;
}

/** 親機ごとの最終ハートビート(epoch 秒)。 */
function heartbeats_(props) {
  const all = props.getProperties ? props.getProperties() : {};
  const out = {};
  Object.keys(all).forEach(function (k) {
    if (k.indexOf('hb_') === 0) out[k.slice(3)] = Number(all[k]);
  });
  return out;
}

/** 途絶えた親機 dead の担当(その親機の地点のノードを除く)を、動いている親機へ移す。移したノードの一覧を返す。 */
function failover_(ss, props, dead, hbs, failoverMin) {
  const now = Date.now() / 1000;
  const alive = Object.keys(hbs)
    .filter(function (p) { return p !== dead && (now - hbs[p]) / 60 <= failoverMin; })
    .sort(function (a, b) { return hbs[b] - hbs[a]; });
  if (!alive.length) return { to: null, nodes: [] };
  const to = alive[0];
  const local = props.getProperty('local_' + dead);
  const sh = ss.getSheetByName(SHEET_ASSIGN);
  if (!sh || sh.getLastRow() < 2) return { to: to, nodes: [] };
  const n = sh.getLastRow() - 1;
  const rows = sh.getRange(2, 1, n, ASSIGN_HEADER.length).getValues();
  const lastStr = Utilities.formatDate(new Date(hbs[dead] * 1000), TZ, 'MM-dd HH:mm');
  const moved = [];
  rows.forEach(function (row) {
    if (String(row[1]).trim() === dead && String(row[0]) !== String(local)) {
      row[1] = to;
      row[2] = new Date();
      row[3] = '自動: ' + dead + ' 途絶(最終 ' + lastStr + ')→ ' + to;
      moved.push(row[0]);
    }
  });
  if (moved.length) {
    sh.getRange(2, 1, n, ASSIGN_HEADER.length).setValues(rows);
    sh.getRange(2, 3, n, 1).setNumberFormat('yyyy-mm-dd hh:mm:ss');
  }
  return { to: to, nodes: moved };
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ------------------------------------------------------------------ alert

/**
 * 時間主導トリガー(10分ごと)から呼ばれる。親機ごとに、
 * - FAILOVER_MIN 分途絶えたら、その親機の担当を動いている親機へ移す(1 回だけ。戻すのは手動)
 * - STALE_MIN 分途絶えたら途絶のメール、戻ったら復旧のメール
 */
function checkStale() {
  const props = PropertiesService.getScriptProperties();
  const hbs = heartbeats_(props);
  const staleMin = Number(props.getProperty('STALE_MIN') || DEFAULT_STALE_MIN);
  const failoverMin = Number(props.getProperty('FAILOVER_MIN') || DEFAULT_FAILOVER_MIN);
  const to = props.getProperty('ALERT_EMAIL') || Session.getEffectiveUser().getEmail();
  const now = Date.now() / 1000;
  let ss = null;

  Object.keys(hbs).sort().forEach(function (pid) {
    const ageMin = (now - hbs[pid]) / 60;
    const lastStr = Utilities.formatDate(new Date(hbs[pid] * 1000), TZ, 'yyyy-MM-dd HH:mm:ss');
    ss = ss || ss_();

    const failed = props.getProperty('failover_' + pid) === '1';
    if (ageMin > failoverMin && !failed) {
      const r = failover_(ss, props, pid, hbs, failoverMin);
      if (r.to) {
        props.setProperty('failover_' + pid, '1');
        if (r.nodes.length) {
          MailApp.sendEmail(to, '[田んぼダム] ' + pid + ' の担当を ' + r.to + ' に移しました',
            pid + ' の最後のハートビート: ' + lastStr + '(' + Math.round(ageMin) + ' 分前)\n' +
            '移したノード: ' + r.nodes.join(', ') + '\n' +
            pid + ' が戻っても自動では戻しません。戻すときは「分担」シートの担当親機を書き換えてください。\n' +
            ss.getUrl());
        }
      }
    } else if (ageMin <= failoverMin && failed) {
      props.setProperty('failover_' + pid, '0');   // 次に途絶えたらまた移せるように(担当は戻さない)
    }

    const alerted = props.getProperty('stale_alerted_' + pid) === '1';
    if (ageMin > staleMin && !alerted) {
      MailApp.sendEmail(to, '[田んぼダム] ' + pid + ' からの送信が途絶えています',
        '最後のハートビート: ' + lastStr + '(' + Math.round(ageMin) + ' 分前)\n' +
        '電源・回線・親機サービスを確認してください。\n' + ss.getUrl());
      props.setProperty('stale_alerted_' + pid, '1');
    } else if (ageMin <= staleMin && alerted) {
      MailApp.sendEmail(to, '[田んぼダム] ' + pid + ' からの送信が復旧しました',
        '最新のハートビート: ' + lastStr + '\n' +
        '担当を移していた場合、自動では戻りません。「分担」シートを確認してください。\n' + ss.getUrl());
      props.setProperty('stale_alerted_' + pid, '0');
    }
  });
}

// ------------------------------------------------------------------ setup

/** 初回に1回だけエディタから実行する。何度実行してもよい(既存のシートとデータは消さない)。 */
function setup() {
  const ss = ss_();
  ss.setSpreadsheetTimeZone(TZ);
  getOrCreateSheet_(ss, SHEET_LATEST, LATEST_HEADER);
  getOrCreateSheet_(ss, SHEET_PARENT, ['項目', DEFAULT_PARENT]);
  if (!ss.getSheetByName(SHEET_ASSIGN)) {
    const sh = getOrCreateSheet_(ss, SHEET_ASSIGN, ASSIGN_HEADER);
    const now = new Date();
    sh.getRange(2, 1, INITIAL_ASSIGN.length, ASSIGN_HEADER.length)
      .setValues(INITIAL_ASSIGN.map(function (a) { return [a[0], a[1], now, a[2]]; }));
    sh.getRange(2, 3, INITIAL_ASSIGN.length, 1).setNumberFormat('yyyy-mm-dd hh:mm:ss');
  }
  writeHelp_(ss);

  // 作成直後の空の既定シート(シート1 / Sheet1)を片付ける
  ss.getSheets().forEach(function (sh) {
    if (['シート1', 'Sheet1'].indexOf(sh.getName()) >= 0 && sh.getLastRow() === 0 &&
        ss.getSheets().length > 1) {
      ss.deleteSheet(sh);
    }
  });
  // 並び順: 最新, 親機, 分担, 説明, (月別シート…)
  [SHEET_LATEST, SHEET_PARENT, SHEET_ASSIGN, SHEET_HELP].forEach(function (name, i) {
    ss.setActiveSheet(ss.getSheetByName(name));
    ss.moveActiveSheet(i + 1);
  });

  const props = PropertiesService.getScriptProperties();
  if (!props.getProperty('TOKEN')) {
    props.setProperty('TOKEN', Utilities.getUuid().replace(/-/g, ''));
  }
  console.log('書き込み先: ' + ss.getName() + ' ' + ss.getUrl());
  console.log('TOKEN = ' + props.getProperty('TOKEN') + '  ← 親機の [upload] token に設定');

  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'checkStale'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('checkStale').timeBased().everyMinutes(10).create();
  console.log('checkStale トリガーを設定しました');
}

/** 「説明」シート: 列と status の意味。setup() のたびに書き直す。 */
function writeHelp_(ss) {
  const sh = ss.getSheetByName(SHEET_HELP) || ss.insertSheet(SHEET_HELP);
  const rows = [
    ['シート / 列', '意味'],
    ['data_YYYY-MM', '計測。1ノード1サイクル1行。記録時刻(JST)の年月で分かれる'],
    ['health_YYYY-MM', '親機の状態。1サイクル1行'],
    ['最新', 'ノードごとの最新状態、最後に OK だった時刻と距離、連続失敗回数'],
    ['親機', '親機ごとに 1 列。最新のハートビート。40 分途絶えるとメール通知'],
    ['分担', 'ノードごとの担当親機。親機はここで自分の担当になっているノードだけを測る。担当親機の列は手で書き換えてよい。' +
      '親機が 30 分途絶えると、その親機の担当(親機の地点のノードを除く)を動いている親機へ自動で移す。戻すのは手動'],
    ['', ''],
    ['Parent / 親機', 'その行を記録した親機(parent_id)'],
    ['Timestamp', '親機が応答を受けた時刻'],
    ['CycleTime', '計測サイクルの予定時刻(全ノード共通。ノード間の比較はこちらで)'],
    ['Dist20_cm', '20℃(音速 343.4 m/s)固定の名目距離。温度補正は EchoMed_us から後処理で'],
    ['EchoMed_us / Min / Max', '有効エコー時間(往復 µs)の中央値・最小・最大。距離[cm] = us × c[m/s] / 20000'],
    ['Valid / Tries', '1回の要求でのバースト測定の有効回数 / 試行回数'],
    ['TxStatus', 'XBee の配送ステータス(10進)。0 = 成功、36 (0x24) = 宛先が見つからない(電源断・未参加)、33 (0x21) = 経路上で ACK なし など'],
    ['UploadErr', '送信エラー。NET = 回線、TIMEOUT = 時間切れ、HTTP = 4xx/5xx、RESP = JSON でない応答、GAS = 受理されず、ERR = その他'],
    ['ClockSynced', '記録時に親機の時計が NTP 同期済みだったか(1/0)'],
    ['ParentRowId', '親機 DB の行 ID(重複排除用)'],
    ['', ''],
    ['OK', '正常'],
    ['NO_ECHO', '子機は応答したが有効エコーが 0 回(水面が測定範囲外、管内の障害物など)'],
    ['OK_V1 / NO_DATA', '旧ファームの子機からの応答'],
    ['TX_FAIL', '子機まで届かなかった(電源断、電波、XBee の不調)'],
    ['TIMEOUT', '子機には届いたが応答なし'],
    ['BAD_REPLY', '解析できない応答(Raw に原文)'],
    ['RADIO_ERR', '親機の XBee が使えない'],
    ['PORT_ERR', '親機の地点のセンサ(USB の ESP32、または GPIO 直結の HC-SR04)が使えない'],
  ];
  sh.clear();
  sh.getRange(1, 1, rows.length, 2).setValues(rows);
  sh.getRange(1, 1, 1, 2).setFontWeight('bold');
  sh.setFrozenRows(1);
}
