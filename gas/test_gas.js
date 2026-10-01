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
        row.forEach((x, j) => { this.sh.data[this.r + i - 1][this.c + j - 1] = x; });
      });
      return this;
    }
    getValues() {
      const out = [];
      for (let i = 0; i < this.nr; i++) {
        const row = this.sh.data[this.r + i - 1] || [];
        out.push(Array.from({ length: this.nc }, (_, j) => row[this.c + j - 1] ?? ''));
      }
      return out;
    }
    setNumberFormat() { return this; }
    setFontWeight() { return this; }
  }
  class Sheet {
    constructor(name) { this.name = name; this.data = []; this.maxRows = 1000; }
    getName() { return this.name; }
    clear() { this.data = []; }
    getRange(r, c, nr, nc) {
      if (typeof r === 'string') return new Range(this, 1, 1, 1, 1);
      return new Range(this, r, c, nr || 1, nc || 1);
    }
    getLastRow() { return this.data.length; }
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
        return fmt.replace('yyyy', p.year).replace('MM', p.month).replace('dd', p.day)
          .replace('HH', p.hour).replace('mm', p.minute).replace('ss', p.second);
      },
      getUuid: () => '11111111-2222-3333-4444-555555555555',
    },
    MailApp: { sendEmail: (to, subj) => mails.push([to, subj]) },
    Session: { getEffectiveUser: () => ({ getEmail: () => 'me@example.com' }) },
    ScriptApp: {
      getProjectTriggers: () => [],
      deleteTrigger: () => {},
      newTrigger: () => ({ timeBased() { return this; }, everyMinutes() { return this; }, create() {} }),
    },
    Date, JSON, Math, Number, String, Array, Object,
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(__dirname + '/Code.gs', 'utf8'), ctx);
  return { ctx, props, sheets, mails, order, opened, getTz: () => tz };
}

function post(env, body) {
  const out = env.ctx.doPost({ postData: { contents: JSON.stringify(body) } });
  return JSON.parse(out.body);
}

const payloads = JSON.parse(fs.readFileSync(__dirname + '/test_payloads.json', 'utf8'));
const env = makeEnv();
env.ctx.setup();
assert.strictEqual(env.getTz(), 'Asia/Tokyo');
assert.deepStrictEqual(env.order, ['最新', '親機', '説明']);          // 空のシート1は削除
assert.strictEqual(env.opened[0], '1UDnr3DmX6d8fwvJzFMTeNSNxzkm9DyW7RCXji2xLfSQ');
assert.ok(env.sheets['説明'].data.length > 10);
env.ctx.setup();                                                      // 2回目も安全
assert.deepStrictEqual(env.order, ['最新', '親機', '説明']);
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
env.props.last_health_ts = String(Date.now() / 1000 - 3600);
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
assert.deepStrictEqual(env.order.slice(0, 3), ['最新', '親機', '説明']);
// SPREADSHEET_ID プロパティで書き込み先を上書きできる
env.props.SPREADSHEET_ID = 'other';
post(env, { ...health, token, rows: [{ ...health.rows[0], id: 100, ts: Date.now() / 1000 }] });
assert.strictEqual(env.opened[env.opened.length - 1], 'other');
console.log('GAS tests passed');
