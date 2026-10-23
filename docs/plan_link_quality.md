# 計画: 電波の強さ(RSSI)の記録と、届きにくさの対策(2026-10-23)

別のセッションで実装するための計画。ここだけ読めば作業できるように書く。

## 背景(2026-10-23 の現地)

- par2(XBee 12、コーディネータ)を旧 node 4 の地点に置いた。親機の地点のセンサ node 10 は 82/82 OK
- 子機を南の 2 点(par2 から約 140 m と約 300 m)に置こうとしたが届かなかった。旧 node 2 の地点(北へ約 86 m)も不安定
- 今の配置: node 2 は par2 のすぐ脇(15:46 以降すべて OK)、node 1 は旧 node 2 の地点
- node 1 の 3 時間の結果: OK 34 / TIMEOUT 40 / TX_FAIL 8(82 回)。**失敗の多くは TIMEOUT**(REQ は配送成功 = 行きは届くのに、応答 = 帰りが落ちる)
- 2 波モデルでの見積もり: アンテナの高さ 0.5 m では 86 m で余裕が数 dB しかない。2 m に上げると 300 m でも約 20 dB 得をする。
  対策を選ぶ前に、行きと帰りの電波の強さを実測で記録したい(卒論の評価にも使う)

## 0. 再送の回数

親機には **TIMEOUT / TX_FAIL のとき同じサイクルで送り直す機能がある**(`poller.poll_with_retry`、設定 `[xbee] retries`、0〜5)。
2026-10-23 17:33 に par2 に入れた(既定 1)。それより前のログ(TIMEOUT のサイクルが約 6.3 s、送り直しのログなし)は入れる前のもの。
入れたあとの 17:40〜18:20 は毎回 3/3 OK で、まだ送り直す場面がなかった。

- **既定を 2 にする**(設定に書かなくても 2)。帰りの成功率が 4 割なら、1 サイクルの成功率は 1 回で約 64%、2 回で約 78% の見込み(各回が独立と仮定)

## 1. RSSI の記録(親機だけの変更。子機のファームは変えない)

### 測るもの

| 列 | 向き | 読み方 |
|---|---|---|
| `rssi_up` | 帰り(子機 → 親機) | 応答を受け取った直後に、親機の XBee のローカル AT `DB` を読む |
| `rssi_down` | 行き(親機 → 子機) | 親機から子機の XBee へリモート AT `DB`(0x17 → 0x97)。答えるのは子機の XBee 自身なので ESP32 は関係ない |
| `tries` | — | そのサイクルで送った REQ の回数(送り直しを含む)。送り直しの効き目を後で数えるため |
| `tx_retries` | 行き | 最後の REQ の Transmit Status(0x8B)の送信のやり直し回数。`radio.py` は `TxStatusEvent.retries` で受け取り済みで、記録するだけ。**無線を増やさずに**行きの苦労が分かる |

- `DB` は「最後に受け取った RF パケットの強さ」。値は正の 16 進(例 0x28)で、意味は −40 dBm。**負の整数の dBm で保存する**
- `DB` は**最後の 1 区間**の値(中継を経由していればすぐ隣の機器との間)。今は直接届いている前提
- ローカル `DB` は、応答の直後に別のパケット(迷子の応答、経路探索)が届くとそちらの値になる。ノードは 1 台ずつ順に測るので、ほぼ問題ない
- リモート `DB` は無線を 1 往復増やす。**配送が成功した(tx_status == 0)ときだけ**読む。TIMEOUT でも配送成功なら読みに行く
  (「行きは届く」の検証になる。ただし帰りが落ちる子機ではリモート AT の応答も落ちやすい → 読めなければ None)

### 変更するファイル

1. `parent/tanbo/radio.py`
   - `local_rssi() -> Optional[int]`: `dev.get_parameter("DB")` → `-int.from_bytes(v, "big")`。失敗は None(RadioError にしない)
   - `remote_rssi(mac, timeout_s) -> Optional[int]`: `RemoteXBeeDevice(dev, XBee64BitAddress.from_hex_string(mac)).get_parameter("DB")`。
     同期操作の時間切れ(`dev.set_sync_ops_timeout`)を一時的に timeout_s にして、終わったら戻す。例外は None
   - どちらも `self._lock` を取る(send と同じ)。受信コールバックは別スレッドなので止まらない
2. `parent/tanbo/config.py` / `parent/config/parent.example.toml`
   - `[xbee] rssi = true`(ローカル DB)、`rssi_remote = true`(リモート DB)、`rssi_timeout_s = 1.5`。検証を足す
3. `parent/tanbo/poller.py`
   - `poll_one`: 応答を採用したら(OK / NO_ECHO / OK_V1 / NO_DATA / BAD_REPLY)、`rssi_up = radio.local_rssi()`
   - `poll_with_retry` の最後に、tx_status == 0 なら `rssi_down = radio.remote_rssi(mac, cfg.xbee.rssi_timeout_s)`。`row["tries"]` に送った回数
   - `poll_one`: Transmit Status を受けたら `row["tx_retries"] = ev.retries`
   - 親機の地点のノード(`poll_local`)は全部 None
   - サイクルの所要時間が延びるのは最大で(ノード数 × rssi_timeout_s)。10 分周期なら問題ない。設置チェック(60 秒周期)でも 3 台なら収まる
4. `parent/tanbo/store.py`
   - `MEAS_COLS` の末尾に `rssi_up`, `rssi_down`, `tries`, `tx_retries`。`SCHEMA` の CREATE にも足す
   - **既存の DB に列を足す**: 起動時に `PRAGMA table_info(measurements)` を見て、無い列を `ALTER TABLE measurements ADD COLUMN` する
     (現地の par2 と p3 の DB は既存。未送信の行が残っていても壊さない)
   - uploader は `MEAS_COLS` の列をそのまま送るので変更不要の見込み(確認する)
5. `parent/tanbo/web.py` / `parent/static/index.html`
   - ノードの表に「RSSI 帰り / 行き」、`/api/history` の cols に `rssi_up`, `rssi_down`, `tries`, `tx_retries`
6. `gas/Code.gs`(VERSION 2.4.0)
   - `MEAS_HEADER` の**末尾**に `RSSI_up_dBm`, `RSSI_down_dBm`, `Tries`, `TxRetries`(既存の月別シートは見出しだけ足す仕組みがある)。`measRow_` に追加
   - 「最新」の `LATEST_HEADER` の末尾に `RSSI_up_dBm`, `RSSI_down_dBm`(`updateLatest_` の空行の初期値の列数も合わせる)
   - 「説明」シートに 3 列の意味を足す
   - `Dashboard.html`: ノードの表に RSSI の列(行き / 帰り)。無い値は「—」
7. `parent/tests/xbee_emu.py`
   - ローカル AT `DB` の応答(`AT_VALUES` に足す、または子機ごとの最後の値)
   - **リモート AT(0x17)を受けて 0x97 を返す**処理を足す。子機モデルに `rssi`(例 60 → −60 dBm)と「リモート AT に答えない」(lossy 用)を持たせる

### テスト

- 親機(`cd parent && python -m pytest -q tests`。pty を使うので Windows では動かない。**p3 か par2 の上で実行**。この PC に WSL は無い)
  - OK のノードに `rssi_up` / `rssi_down` / `tries` が入る。TX_FAIL は rssi が None、tries は retries + 1
  - リモート AT に答えない子機でも、サイクルが止まらず `rssi_down` が None
  - `rssi = false` で読まない
  - 列の無い古い DB(今の SCHEMA で作ったもの)を開くと列が足され、既存の行と未送信の行が残る
  - 送信の payload に新しい列が入る(偽 GAS で確認)
- GAS(`node gas/test_gas.js`)
  - 新しい列が `data_` と「最新」に入る。列を足す前の月別シートには見出しだけ足す
  - `dashData` の nodes に rssi が入る(Date を含まない)
- 画面(`Dashboard.html`)は、見本データで null / undefined のどちらでも表示が崩れないこと(10-23 に google.script.run が null の項目を落とす件で一度壊れた)

### 入れ方(現地の par2。sudo はユーザー)

1. コミット・push(ユーザーの承認後)
2. par2: `cd ~/tanbo_dam && git pull`
3. `sudo cp -r ~/tanbo_dam/parent/* /opt/tanbo/ && sudo /opt/tanbo/venv/bin/pip install -q "/opt/tanbo[gpio]" && sudo systemctl restart tanbo-parent tanbo-web`
4. 設定の変更は要らない(retries の既定は 2、rssi は既定で読む)。止めたいときだけ `rssi*` を書く
5. GAS: `Code.gs` と `Dashboard.html` を貼り替え → 受信用と画面用の**両方のデプロイ**を新バージョンに
6. 確認: par2 のログ、`/api/overview` の rssi、スプレッドシートの新しい列

## 2. 記録してから行う現地の実験(実装のあと)

1. **今の配置のまま 1〜2 日**: node 1(86 m)と node 2(脇)の `rssi_up` / `rssi_down`、送り直しの回数、成功率。行きと帰りの差を確かめる
2. **高さの実験**: node 1 と par2 の XBee(箱ごと、または XBee だけ)を 0.5 m → 1 m → 2 m に上げる。
   各段で設置チェック(60 秒周期)を 30 分。成功率と RSSI を比べる。親機の XBee を USB 延長で Pi・通信端末から離す試験も同じ形で
3. **モデル合わせ**: 実測の RSSI を 2 波モデル(送信 +8 dBm、感度 −103 dBm、高さ、追加の損失)と比べ、追加の損失を推定する。
   その上で、中継の置き方、外付けアンテナ、920 MHz LoRa への変更などを判断する

## 3. 記録・ドキュメント

- `docs/pi_notes.md` に、現地の結果(上の背景)、retries の確認結果、RSSI の初日の値を追記
- README の設定の説明に `retries` と `rssi*`。CLAUDE.md の構成(poller / radio)に RSSI の一行
- Vault(ユーザーが頼んだら): 「XBeeネットワークと宛先」か新しいノート「電波の届き方」に、モデルと実測

## 決めたこと(2026-10-23、ユーザー)

- 送り直しの既定は 2 回
- リモート `DB` は毎サイクル読む(既定 `rssi_remote = true`)。困ったら false。行きの品質は `tx_retries` でも見る
