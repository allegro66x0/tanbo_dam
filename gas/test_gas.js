// Code.gs を Node 上の簡易 Apps Script モックで検証する。 node gas/test_gas.js
'use strict';
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

function makeEnv() {
  const props = {};
  const mails = [];
  const sheets = {};
  let tz = 'America/New_York';

  class Range {
    constructor(sh, r, c, nr, nc) { Object.assign(this, { sh, r, c, nr, nc }); }
    setValues(v) {
      assert.strictEqual(v.length, this.nr, 'row count');
      v.forEach((row, i) => {
        assert.strictEqual(row.length, this.nc, 'col count');
        if (this.r + i > this.sh.maxRows) throw new Error('outside the dimensions of the sheet');
        this.sh.data[this.r + i - 1] = this.sh.data[this.r + i - 1] || [];
        row.forEach((x, j) => {
          const auto = typeof x === 'string' && /^\d{1,2}\/\d{1,2}$/.test(x) && this.sh.fmtOf(this.r + i, this.c + j) !== '@';
          this.sh.data[this.r + i - 1][this.c + j - 1] = auto
            ? new Date(Date.UTC(2026, Number(x.split('/')[0]) - 1, Number(x.split('/')[1])) - 9 * 3600000) : x;
        });
      });
      return this;
    }
    getValues() {
      const out = [];
      for (let i = 0; i < this.nr; i++) {
        const row = this.sh.data[this.r + i - 1] || [];
        out.push(Array.from({ length: this.nc }, (_, j) => {
          const x = row[this.c + j - 1] ?? '';
          const fmt = this.sh.fmtOf(this.r + i, this.c + j) || '';
          // シリアル値 → スプレッドシートのタイムゾーン(JST)の 0 時の Date
          return (typeof x === 'number' && fmt.includes('yyyy'))
            ? new Date(Date.UTC(1899, 11, 30) + x * 86400000 - 9 * 3600000) : x;
        }));
      }
      return out;
    }
    setNumberFormat(f) {
      const whole = this.r === 1 && this.nr >= this.sh.maxRows;   // 'A:B' のような列全体
      for (let j = 0; j < this.nc; j++) {
        if (whole) this.sh.fmt[this.c + j] = f;
        else for (let i = 0; i < this.nr; i++) this.sh.cfmt[(this.r + i) + ':' + (this.c + j)] = f;
      }
      return this;
    }
    setFontWeight() { return this; }
    clearFormat() {
      for (let j = 0; j < this.nc; j++) {
        delete this.sh.fmt[this.c + j];
        for (let i = 0; i < this.nr; i++) delete this.sh.cfmt[(this.r + i) + ':' + (this.c + j)];
      }
      return this;
    }
    clearContent() {
      for (let i = 0; i < this.nr; i++) {
        const row = this.sh.data[this.r + i - 1];
        if (row) for (let j = 0; j < this.nc; j++) row[this.c + j - 1] = '';
      }
      while (this.sh.data.length && this.sh.data[this.sh.data.length - 1].every((x) => x === '')) this.sh.data.pop();
      return this;
    }
  }
  class Sheet {
    constructor(name) { this.name = name; this.data = []; this.maxRows = 1000; this.fmt = {}; this.cfmt = {}; }
    fmtOf(r, c) { const k = r + ':' + c; return k in this.cfmt ? this.cfmt[k] : this.fmt[c]; }
    getName() { return this.name; }
    clear() { this.data = []; }
    getRange(r, c, nr, nc) {
      if (typeof r === 'string') {   // 'A:B'(列全体)や 'B3:Z30'
        const m = r.match(/^([A-Z])(\d*):([A-Z])(\d*)$/);
        const c1 = m[1].charCodeAt(0) - 64;
        const nc2 = m[3].charCodeAt(0) - 64 - c1 + 1;
        if (!m[2]) return new Range(this, 1, c1, this.maxRows, nc2);
        return new Range(this, Number(m[2]), c1, Number(m[4]) - Number(m[2]) + 1, nc2);
      }
      return new Range(this, r, c, nr || 1, nc || 1);
    }
    getLastRow() { return this.data.length; }
    getLastColumn() { return this.data.reduce((m, r) => Math.max(m, (r || []).length), 0); }
    getMaxRows() { return this.maxRows; }
    insertRowsAfter(_, n) { this.maxRows += n; }
    setFrozenRows() {}
  }
  const order = ['シート1'];
  sheets['シート1'] = new Sheet('シート1');
  const opened = [];
  let active = null;
  const ss = {
    getName: () => 'tambo_data2',
    getSheets: () => order.map((n) => sheets[n]),
    deleteSheet: (sh) => { delete sheets[sh.name]; order.splice(order.indexOf(sh.name), 1); },
    setActiveSheet: (sh) => { active = sh; },
    moveActiveSheet: (pos) => { order.splice(order.indexOf(active.name), 1); order.splice(pos - 1, 0, active.name); },
    getSheetByName: (n) => sheets[n] || null,
    insertSheet: (n) => { order.push(n); return (sheets[n] = new Sheet(n)); },
    setSpreadsheetTimeZone: (t) => { tz = t; },
    getUrl: () => 'https://example/ss',
  };
  const ctx = {
    console,
    SpreadsheetApp: { openById: (id) => { opened.push(id); return ss; } },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (k) => (k in props ? props[k] : null),
        setProperty: (k, v) => { props[k] = String(v); },
        getProperties: () => ({ ...props }),
      }),
    },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    ContentService: {
      MimeType: { JSON: 'json' },
      createTextOutput: (s) => ({ body: s, setMimeType() { return this; } }),
    },
    Utilities: {
      formatDate: (d, zone, fmt) => {
        const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
          timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
        }).formatToParts(d).map((x) => [x.type, x.value]));
        if (fmt === 'M/d') return Number(p.month) + '/' + Number(p.day);
        return fmt.replace('yyyy', p.year).replace('MM', p.month).replace('dd', p.day)
          .replace('HH', p.hour).replace('mm', p.minute).replace('ss', p.second);
      },
      getUuid: () => '11111111-2222-3333-4444-555555555555',
    },
    MailApp: { sendEmail: (to, subj) => mails.push([to, subj]) },
    Session: {
      getEffectiveUser: () => ({ getEmail: () => 'me@example.com' }),
      getActiveUser: () => ({ getEmail: () => env.activeUser }),
    },
    HtmlService: {
      createHtmlOutputFromFile: (f) => ({
        file: f, setTitle() { return this; }, addMetaTag() { return this; },
      }),
    },
    ScriptApp: {
      getProjectTriggers: () => [],
      deleteTrigger: () => {},
      newTrigger: () => ({ timeBased() { return this; }, everyMinutes() { return this; }, create() {} }),
    },
    Date, JSON, Math, Number, String, Array, Object,
  };
  const env = { ctx, props, sheets, mails, order, opened, getTz: () => tz, activeUser: '' };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(__dirname + '/Code.gs', 'utf8'), ctx);
  return env;
}

function post(env, body) {
  const out = env.ctx.doPost({ postData: { contents: JSON.stringify(body) } });
  return JSON.parse(out.body);
}

const payloads = JSON.parse(fs.readFileSync(__dirname + '/test_payloads.json', 'utf8'));
const env = makeEnv();
env.ctx.setup();
assert.strictEqual(env.getTz(), 'Asia/Tokyo');
assert.deepStrictEqual(env.order, ['最新', '親機', '分担', '説明']);  // 空のシート1は削除
assert.strictEqual(env.opened[0], '1UDnr3DmX6d8fwvJzFMTeNSNxzkm9DyW7RCXji2xLfSQ');
assert.ok(env.sheets['説明'].data.length > 10);
env.ctx.setup();                                                      // 2回目も安全
assert.deepStrictEqual(env.order, ['最新', '親機', '分担', '説明']);
const token = env.props.TOKEN;
assert.ok(token && token.length === 32);

// 認証と入力検証
assert.deepStrictEqual(post(env, { ...payloads[0], token: 'x' }), { ok: false, error: 'bad token' });
assert.strictEqual(post(env, { ...payloads[0], token, kind: 'z' }).error, 'bad kind');
assert.strictEqual(post(env, { ...payloads[0], token, db_uuid: '../x' }).error, 'bad db_uuid');
assert.strictEqual(JSON.parse(env.ctx.doPost({ postData: { contents: '{' } }).body).error, 'invalid JSON');

// 実際の親機 payload(health → measurements)
for (const p of payloads) {
  const r = post(env, { ...p, token });
  assert.ok(r.ok, JSON.stringify(r));
  assert.strictEqual(r.added, p.rows.length);
  assert.strictEqual(r.hwm, p.rows[p.rows.length - 1].id);
}
const meas = payloads.find((p) => p.kind === 'm');
const month = env.ctx.Utilities.formatDate(new Date(meas.rows[0].ts * 1000), 'Asia/Tokyo', 'yyyy-MM');
const data = env.sheets['data_' + month];
assert.ok(data, 'monthly data sheet');
assert.strictEqual(data.getLastRow(), 1 + meas.rows.length);
assert.deepStrictEqual(data.data[0].slice(0, 5), ['Timestamp', 'CycleTime', 'Node', 'Status', 'Dist20_cm']);
const row1 = data.data[1];
assert.ok(row1[0] instanceof Date);
assert.strictEqual(row1[2], 0);
assert.strictEqual(row1[3], 'OK');
assert.strictEqual(row1[4], 60.1);
assert.strictEqual(row1[16], 1);
assert.strictEqual(row1[18], 'parent-1');                          // 記録した親機

// 再送(同じ行)は追加されない
const again = post(env, { ...meas, token });
assert.deepStrictEqual([again.ok, again.added], [true, 0]);
assert.strictEqual(data.getLastRow(), 1 + meas.rows.length);

// 一部だけ新しい行を含む再送
const extra = { ...meas.rows[0], id: meas.rows[meas.rows.length - 1].id + 1, status: 'TIMEOUT',
  dist_cm: null, med_us: null, n_ok: null, n_try: null };
const r2 = post(env, { ...meas, token, rows: meas.rows.concat([extra]) });
assert.deepStrictEqual([r2.added, r2.hwm], [1, extra.id]);

// 「最新」シート: ノード0は最新が TIMEOUT、最終OK距離は保持、連続失敗 1
const latest = env.sheets['最新'];
const n0 = latest.data.find((r) => r[0] === 0);
assert.strictEqual(n0[2], 'TIMEOUT');
assert.strictEqual(n0[7], 60.1);
assert.strictEqual(n0[8], 1);
const n2 = latest.data.find((r) => r[0] === 2);
assert.strictEqual(n2[2], 'TX_FAIL');
assert.strictEqual(n2[8], 2);       // 2サイクル連続失敗
assert.strictEqual(n2[6], '');      // 一度も OK なし
// ノード順に並ぶ
assert.deepStrictEqual(latest.data.slice(1).map((r) => r[0]), [0, 1, 2, 3, 4, 5]);

// 別 DB(親機の SD を作り直した場合)は id が 1 からでも受け付ける
const r3 = post(env, { ...meas, token, db_uuid: 'abcdef0123456789' });
assert.strictEqual(r3.added, meas.rows.length);

// 月またぎ: 2行が別シートへ
const t1 = Date.UTC(2026, 9, 31, 14, 59, 0) / 1000;   // JST 10/31 23:59
const t2 = t1 + 120;                                   // JST 11/01 00:01
const r4 = post(env, { ...meas, token, db_uuid: 'feedfeedfeed', rows: [
  { ...meas.rows[0], id: 1, ts: t1 }, { ...meas.rows[0], id: 2, ts: t2 }] });
assert.strictEqual(r4.added, 2);
assert.ok(env.sheets['data_2026-10'] && env.sheets['data_2026-11']);

// 行数が足りなくなったら拡張する
const big = [];
for (let i = 1; i <= 1200; i++) big.push({ ...meas.rows[0], id: i, ts: t2 + i });
const r5 = post(env, { ...meas, token, db_uuid: 'b16b16b16b16', rows: big });
assert.strictEqual(r5.added, 1200);
assert.ok(env.sheets['data_2026-11'].getMaxRows() >= 1202);

// 親機シートとアラート
const health = payloads.find((p) => p.kind === 'h');
const parent = env.sheets['親機'];
assert.strictEqual(parent.data[1][0], '最終ハートビート');
env.props['hb_parent-1'] = String(Date.now() / 1000 - 3600);
env.ctx.checkStale();
env.ctx.checkStale();
assert.strictEqual(env.mails.length, 1);
assert.ok(env.mails[0][1].includes('途絶'));
post(env, { ...health, token, rows: [{ ...health.rows[0], id: 99, ts: Date.now() / 1000 }] });
env.ctx.checkStale();
assert.strictEqual(env.mails.length, 2);
assert.ok(env.mails[1][1].includes('復旧'));

assert.strictEqual(JSON.parse(env.ctx.doGet().body).ok, true);
// 月別シートは説明シートの後ろに並ぶ
assert.deepStrictEqual(env.order.slice(0, 4), ['最新', '親機', '分担', '説明']);
// SPREADSHEET_ID プロパティで書き込み先を上書きできる
env.props.SPREADSHEET_ID = 'other';
post(env, { ...health, token, rows: [{ ...health.rows[0], id: 100, ts: Date.now() / 1000 }] });
assert.strictEqual(env.opened[env.opened.length - 1], 'other');

// ---------------------------------------------------------------- 分担表(親機 2 台)
{
  const e = makeEnv();
  e.ctx.setup();
  const tok = e.props.TOKEN;
  const assignSh = e.sheets['分担'];
  assert.deepStrictEqual(assignSh.data.slice(1).map((r) => [r[0], r[1]]),
    [[0, ''], [2, 'parent-1'], [4, 'parent-1'], [10, 'parent-2'], [11, 'parent-1']]);
  const now = Date.now() / 1000;
  const hrow = (id, ts) => ({ ...health.rows[0], id, ts });
  // 応答には毎回分担表が入る
  const a1 = post(e, { ...health, token: tok, parent_id: 'parent-1', local_node: 11, db_uuid: 'aaaa0001', rows: [hrow(1, now)] });
  assert.deepStrictEqual(a1.assign, { 2: 'parent-1', 4: 'parent-1', 10: 'parent-2', 11: 'parent-1' });
  const a2 = post(e, { ...health, token: tok, parent_id: 'parent-2', local_node: 10, db_uuid: 'bbbb0001', rows: [hrow(1, now)] });
  assert.deepStrictEqual(a2.assign, a1.assign);
  assert.strictEqual(e.props.local_parent_1, undefined);
  assert.strictEqual(e.props['local_parent-1'], '11');
  assert.strictEqual(post(e, { ...health, token: tok, parent_id: 'bad id!', rows: [] }).error, 'bad parent_id');
  // 親機シートは親機ごとに 1 列
  const ps = e.sheets['親機'];
  assert.deepStrictEqual(ps.data[0].slice(0, 3), ['項目', 'parent-1', 'parent-2']);
  assert.ok(ps.data[1][1] instanceof Date && ps.data[1][2] instanceof Date);
  // 手で書き換えた分担はそのまま返る
  assignSh.data[2][1] = 'parent-2';
  assert.strictEqual(post(e, { ...health, token: tok, parent_id: 'parent-2', db_uuid: 'bbbb0001', rows: [] }).assign['2'], 'parent-2');
  assignSh.data[2][1] = 'parent-1';

  // parent-1 が 60 分途絶 → 地点(11)以外の担当を parent-2 へ。メール 1 通(移した)+ 途絶 1 通
  e.props['hb_parent-1'] = String(now - 3600);
  e.ctx.checkStale();
  assert.deepStrictEqual(assignSh.data.slice(1).map((r) => [r[0], r[1]]),
    [[0, ''], [2, 'parent-2'], [4, 'parent-2'], [10, 'parent-2'], [11, 'parent-1']]);   // 担当が空の行は動かさない
  assert.ok(String(assignSh.data[2][3]).startsWith('自動: parent-1 途絶'));
  assert.ok(e.mails.some((m) => m[1].includes('parent-1 の担当を parent-2 に移しました')));
  assert.ok(e.mails.some((m) => m[1].includes('parent-1 からの送信が途絶えています')));
  const nMails = e.mails.length;
  e.ctx.checkStale();                                    // 2 回目は何もしない
  assert.strictEqual(e.mails.length, nMails);
  // parent-1 が戻っても、担当は自動では戻らない
  post(e, { ...health, token: tok, parent_id: 'parent-1', db_uuid: 'aaaa0001', rows: [hrow(2, Date.now() / 1000)] });
  e.ctx.checkStale();
  assert.strictEqual(assignSh.data[2][1], 'parent-2');
  assert.ok(e.mails[e.mails.length - 1][1].includes('parent-1 からの送信が復旧しました'));
  assert.strictEqual(e.props['failover_parent-1'], '0');

  // 動いている親機がいなければ移さない
  const f = makeEnv();
  f.ctx.setup();
  f.props['hb_parent-1'] = String(now - 3600);
  f.ctx.checkStale();
  assert.strictEqual(f.sheets['分担'].data[2][1], 'parent-1');

  // 列を足す前に作った月別シートには、見出しだけ足す
  const g = makeEnv();
  g.ctx.setup();
  const month2 = g.ctx.Utilities.formatDate(new Date(meas.rows[0].ts * 1000), 'Asia/Tokyo', 'yyyy-MM');
  const old = g.ctx.SpreadsheetApp.openById('x').insertSheet('data_' + month2);
  old.data.push(['Timestamp', 'CycleTime', 'Node', 'Status', 'Dist20_cm', 'EchoMed_us', 'EchoMin_us',
    'EchoMax_us', 'Valid', 'Tries', 'Seq', 'TxStatus', 'RTT_ms', 'ChildUptime_s', 'FW',
    'ClockSynced', 'ParentRowId', 'Raw']);
  old.data.push(['x']);
  post(g, { ...meas, token: g.props.TOKEN });
  assert.strictEqual(old.data[0][18], 'Parent');
  assert.strictEqual(old.data[1][0], 'x');
  assert.strictEqual(old.data[2][18], 'parent-1');
  // 旧形式の親機シート(項目 / 値)は parent-1 の列として引き継ぐ
  const ps2 = g.sheets['親機'];
  ps2.data[0] = ['項目', '値'];
  post(g, { ...health, token: g.props.TOKEN, parent_id: 'parent-2', db_uuid: 'cccc0001', rows: [hrow(1, now)] });
  assert.deepStrictEqual(ps2.data[0].slice(0, 3), ['項目', 'parent-1', 'parent-2']);
}

// ---------------------------------------------------------------- Node 列に日時の書式が付いていた旧シート(v2.1 の不具合)
{
  const e = makeEnv();
  e.ctx.setup();
  const tok = e.props.TOKEN;
  const lt = e.sheets['最新'];
  const as = e.sheets['分担'];
  assert.strictEqual(lt.fmt[1], '0');                    // 新しく作るシートの Node 列は数値の書式
  assert.strictEqual(as.fmt[1], '0');
  // 旧版の状態を作る: Node 列が日時の書式で、ノード 2 の行が 2 つある
  lt.fmt[1] = 'yyyy-mm-dd hh:mm:ss';
  as.fmt[1] = 'yyyy-mm-dd hh:mm:ss';
  lt.data.push([2, 'x', 'TX_FAIL', '', '', '', '', '', 3, '', ''], [2, 'y', 'TX_FAIL', '', '', '', '', '', 5, '', '']);
  // 日付で読めても分担表のノード番号は正しい
  const res = post(e, { ...meas, token: tok, rows: [{ ...meas.rows[0], id: 1, node: 2, status: 'TX_FAIL' }] });
  assert.deepStrictEqual(res.assign, { 2: 'parent-1', 4: 'parent-1', 10: 'parent-2', 11: 'parent-1' });
  // 「最新」は 1 ノード 1 行にまとまり、連続失敗は最後の行(5)から続く
  const rows2 = lt.data.slice(1).filter((r) => r.some((x) => x !== ''));
  assert.strictEqual(rows2.length, 1);
  assert.strictEqual(rows2[0][0], 2);
  assert.strictEqual(rows2[0][8], 6);
  // setup をもう一度走らせると書式が直る
  e.ctx.setup();
  assert.strictEqual(lt.fmt[1], '0');
  assert.strictEqual(as.fmt[1], '0');
  // 途絶時の移動も、日付で読めた Node 列のまま正しく動く
  as.fmt[1] = 'yyyy-mm-dd hh:mm:ss';
  const now2 = Date.now() / 1000;
  e.props['hb_parent-1'] = String(now2 - 3600);
  e.props['hb_parent-2'] = String(now2);
  e.props['local_parent-1'] = '11';
  e.ctx.checkStale();
  as.fmt[1] = '0';
  assert.deepStrictEqual(as.data.slice(1).map((r) => [r[0], r[1]]),
    [[0, ''], [2, 'parent-2'], [4, 'parent-2'], [10, 'parent-2'], [11, 'parent-1']]);
}

// ---------------------------------------------------------------- 日時でない列の書式(v2.2.2)
{
  const e = makeEnv();
  e.ctx.setup();
  const tok = e.props.TOKEN;
  post(e, { ...health, token: tok });
  post(e, { ...meas, token: tok });
  const lt = e.sheets['最新'];
  const okRow = lt.data.slice(1).find((r) => r[2] === 'OK');
  assert.strictEqual(typeof okRow[5], 'string');           // "7/7" が日付にならない
  assert.ok(/^\d+\/\d+$/.test(okRow[5]));
  const hmonth = e.ctx.Utilities.formatDate(new Date(health.rows[0].ts * 1000), 'Asia/Tokyo', 'yyyy-MM');
  const hs = e.sheets['health_' + hmonth];
  assert.ok(!String(hs.fmt[2] || '').includes('yyyy'));    // health_ の Interval_s は日時の書式にしない
  assert.ok(String(hs.fmt[1]).includes('yyyy'));
  const dmonth = e.ctx.Utilities.formatDate(new Date(meas.rows[0].ts * 1000), 'Asia/Tokyo', 'yyyy-MM');
  assert.ok(String(e.sheets['data_' + dmonth].fmt[2]).includes('yyyy'));   // data_ の CycleTime は日時
  // 旧版の状態: 日付になってしまった Valid/Tries、日時の書式の health_ B 列・親機シートの値の列 → setup と次の書き込みで直る
  okRow[5] = new Date(Date.UTC(2026, 6, 7) - 9 * 3600000);
  lt.fmt[6] = 'yyyy/mm/dd';
  hs.fmt[2] = 'yyyy-mm-dd hh:mm:ss';
  e.sheets['親機'].fmt[2] = 'yyyy-mm-dd hh:mm:ss';
  e.ctx.setup();
  assert.strictEqual(hs.fmt[2], '0');
  assert.strictEqual(e.sheets['親機'].fmtOf(5, 2), '@');           // 周期_s など
  assert.ok(String(e.sheets['親機'].fmtOf(2, 2)).includes('yyyy'));  // 最終ハートビート
  // 親機シートの "1/12"(前回サイクル OK/全体)が日付にならない
  e.sheets['親機'].cfmt = {};   // setup で付けた書式がなくても、書き込むときに付ける
  post(e, { ...health, token: tok, rows: [{ ...health.rows[0], id: 50, ts: Date.now() / 1000, n_ok: 1, n_nodes: 12 }] });
  const okAll = e.sheets['親機'].data.find((r) => r[0] === '前回サイクル OK/全体');
  assert.strictEqual(okAll[1], '1/12');
  assert.strictEqual(lt.fmt[6], '@');
  post(e, { ...meas, token: tok, rows: [{ ...meas.rows[0], id: 999, node: 99, status: 'TIMEOUT' }] });
  const fixed = lt.data.slice(1).find((r) => r[0] === okRow[0]);
  assert.strictEqual(fixed[5], '7/7');
}

// ---------------------------------------------------------------- 既存の分担シートには、無いノードの行だけ足す
{
  const e = makeEnv();
  e.ctx.setup();
  const as = e.sheets['分担'];
  as.data = as.data.filter((r) => r[0] !== 0);          // node 0 を足す前の版で作ったシート
  as.data[1][1] = 'parent-2';                           // 手で書き換えた担当
  e.ctx.setup();
  assert.deepStrictEqual(as.data.slice(1).map((r) => [r[0], r[1]]),
    [[2, 'parent-2'], [4, 'parent-1'], [10, 'parent-2'], [11, 'parent-1'], [0, '']]);
  e.ctx.setup();                                        // 何度実行しても増えない
  assert.strictEqual(as.data.length, 6);
}

// ---------------------------------------------------------------- 表示用の画面(持ち主だけ)
{
  const e = makeEnv();
  e.ctx.setup();
  const tok = e.props.TOKEN;
  const now = Date.now() / 1000;
  post(e, { ...health, token: tok, parent_id: 'parent-1', local_node: 11, db_uuid: 'aaaa0001',
    rows: [{ ...health.rows[0], id: 1, ts: now }] });
  post(e, { ...health, token: tok, parent_id: 'parent-2', local_node: 10, db_uuid: 'bbbb0001',
    rows: [{ ...health.rows[0], id: 1, ts: now }] });
  post(e, { ...meas, token: tok });

  // 匿名(「アクセス: 全員」のデプロイ)には今までどおり JSON、画面の関数は使えない
  assert.strictEqual(JSON.parse(e.ctx.doGet().body).ok, true);
  assert.throws(() => e.ctx.dashData(), /持ち主/);
  assert.throws(() => e.ctx.dashSetAssign(2, 'parent-2', ''), /持ち主/);
  e.activeUser = 'other@example.com';
  assert.throws(() => e.ctx.dashData(), /持ち主/);

  // 持ち主には画面
  e.activeUser = 'me@example.com';
  assert.strictEqual(e.ctx.doGet().file, 'Dashboard');
  const raw = e.ctx.dashData();
  // google.script.run は Date を渡せない → どこにも Date が入っていない(vm の Date も見分けるため toString で判定)
  const hasDate = (x) => Object.prototype.toString.call(x) === '[object Date]' ||
    (x !== null && typeof x === 'object' && Object.values(x).some(hasDate));
  assert.ok(!hasDate(raw));
  const plain = (x) => JSON.parse(JSON.stringify(x));   // google.script.run と同じく JSON を通す
  const d = plain(raw);
  assert.deepStrictEqual(d.nodes.map((n) => n.node), [0, 1, 2, 3, 4, 5]);
  const dn0 = d.nodes.find((n) => n.node === 0);
  assert.strictEqual(dn0.status, 'OK');
  assert.strictEqual(dn0.dist, 60.1);
  assert.ok(typeof dn0.ts === 'number' && /^\d+\/\d+$/.test(dn0.valid));
  assert.deepStrictEqual(Object.keys(d.parents).sort(), ['parent-1', 'parent-2']);
  assert.strictEqual(d.parents['parent-1'].localNode, 11);
  assert.ok(Math.abs(d.parents['parent-2'].hb - now * 1000) < 2000);
  assert.ok(typeof d.parents['parent-1'].items['最終ハートビート'] === 'number');
  // v2.2.1 までに作った親機の列: 日時の書式で "2/3" や数値が日付で読める → 元の値に戻して渡す
  {
    const ps = e.sheets['親機'];
    const row = (label) => ps.data.find((r) => r[0] === label);
    const saved = [row('前回サイクル OK/全体')[1], row('CPU温度_C')[1]];
    row('前回サイクル OK/全体')[1] = new Date(Date.UTC(2026, 1, 3) - 9 * 3600000);           // 2/3
    row('CPU温度_C')[1] = new Date(Date.UTC(1900, 1, 9, 21, 36) - 9 * 3600000);              // シリアル値 41.9
    const it = plain(e.ctx.dashData()).parents['parent-1'].items;
    assert.strictEqual(it['前回サイクル OK/全体'], '2/3');
    assert.strictEqual(it['CPU温度_C'], 41.9);
    row('前回サイクル OK/全体')[1] = saved[0];
    row('CPU温度_C')[1] = saved[1];
  }
  assert.deepStrictEqual(d.assign.map((a) => [a.node, a.parent]),
    [[0, ''], [2, 'parent-1'], [4, 'parent-1'], [10, 'parent-2'], [11, 'parent-1']]);

  // 分担を変える: 既存の行は書き換え、無いノードは末尾に足す。空欄は「測らない」
  const as = e.sheets['分担'];
  e.ctx.dashSetAssign(2, 'parent-2', 'par2 の設置');
  const r2 = as.data.find((r) => r[0] === 2);
  assert.strictEqual(r2[1], 'parent-2');
  assert.ok(r2[2] instanceof Date);
  assert.strictEqual(r2[3], '手動(画面): par2 の設置');
  const d2 = plain(e.ctx.dashSetAssign(3, 'parent-1', ''));
  assert.deepStrictEqual(plain(as.data[as.data.length - 1].slice(0, 2)), [3, 'parent-1']);
  assert.ok(d2.assign.some((a) => a.node === 3 && a.parent === 'parent-1'));
  e.ctx.dashSetAssign(4, '', '');
  assert.strictEqual(as.data.find((r) => r[0] === 4)[1], '');
  assert.strictEqual(as.data.length, 7);                 // 2・4 は書き換え、3 だけ増えた
  // 親機が受け取る分担表にも反映される(空欄の 4 は入らない)
  assert.deepStrictEqual(post(e, { ...health, token: tok, db_uuid: 'aaaa0001', rows: [] }).assign,
    { 2: 'parent-2', 3: 'parent-1', 10: 'parent-2', 11: 'parent-1' });
  assert.throws(() => e.ctx.dashSetAssign(2, 'bad id!', ''), /親機 ID/);
  assert.throws(() => e.ctx.dashSetAssign(-1, 'parent-1', ''), /ノード番号/);
  assert.throws(() => e.ctx.dashSetAssign('x', 'parent-1', ''), /ノード番号/);

  // 親機の操作画面へのリンク: スクリプトプロパティに置く。URL が空の親機は消す
  const d3 = plain(e.ctx.dashSetLinks({ 'parent-1': { name: 'p3', url: 'http://parent-1.example:8080/' },
    'parent-2': { name: 'par2', url: '' } }));
  assert.deepStrictEqual(d3.links, { 'parent-1': { name: 'p3', url: 'http://parent-1.example:8080/' } });
  assert.deepStrictEqual(JSON.parse(e.props.PARENT_LINKS), d3.links);
  assert.throws(() => e.ctx.dashSetLinks({ 'parent-1': { name: 'x', url: 'javascript:alert(1)' } }), /URL/);
  assert.throws(() => e.ctx.dashSetLinks({ 'bad id!': { name: 'x', url: 'http://a/' } }), /親機 ID/);
  e.props.PARENT_LINKS = '{broken';
  assert.deepStrictEqual(plain(e.ctx.dashData().links), {});
}
// ---------------------------------------------------------------- 電波の記録(v2.4.0)
{
  const e = makeEnv();
  e.ctx.setup();
  const tok = e.props.TOKEN;
  const r0 = { ...meas.rows[0], id: 1, node: 1, tries: 2, tx_retries: 1, rssi_up: -71, rssi_down: -68 };
  const r1 = { ...meas.rows[0], id: 2, node: 2 };   // 列を足す前の親機(電波の項目なし)
  assert.ok(post(e, { ...meas, token: tok, db_uuid: 'dddd0001', rows: [r0, r1] }).ok);
  const month3 = e.ctx.Utilities.formatDate(new Date(r0.ts * 1000), 'Asia/Tokyo', 'yyyy-MM');
  const ds = e.sheets['data_' + month3];
  assert.deepStrictEqual(ds.data[0].slice(19), ['Tries', 'TxRetries', 'RSSI_up_dBm', 'RSSI_down_dBm']);
  const row1 = ds.data.find((r) => r[2] === 1);
  assert.deepStrictEqual(row1.slice(18), ['parent-1', 2, 1, -71, -68]);
  assert.deepStrictEqual(ds.data.find((r) => r[2] === 2).slice(19), ['', '', '', '']);
  const lt = e.sheets['最新'];
  assert.deepStrictEqual(lt.data[0].slice(11), ['RSSI_up_dBm', 'RSSI_down_dBm']);
  assert.deepStrictEqual(lt.data.find((r) => r[0] === 1).slice(11), [-71, -68]);
  // 画面: ノードに rssiUp / rssiDown(無ければ null)
  e.activeUser = 'me@example.com';
  const nodes = JSON.parse(JSON.stringify(e.ctx.dashData().nodes));
  assert.deepStrictEqual([nodes.find((n) => n.node === 1).rssiUp, nodes.find((n) => n.node === 1).rssiDown], [-71, -68]);
  assert.strictEqual(nodes.find((n) => n.node === 2).rssiUp, null);
  // 列を足す前に作った「最新」(11 列)にも、見出しと値が足される
  const g = makeEnv();
  g.ctx.setup();
  const lt2 = g.sheets['最新'];
  lt2.data[0] = lt2.data[0].slice(0, 11);
  post(g, { ...meas, token: g.props.TOKEN, db_uuid: 'eeee0001', rows: [r0] });
  assert.deepStrictEqual(lt2.data[0].slice(11), ['RSSI_up_dBm', 'RSSI_down_dBm']);
}
console.log('GAS tests passed');
