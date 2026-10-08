# 田んぼダム 水位計測システム v2

親機(Raspberry Pi 4)・GAS・子機(ESP32)を作り直した版です。プロトコルは `docs/PROTOCOL.md` にあります。

```
child/tanbo_child_v2/   子機ファームウェア(全子機同一)
parent/                 親機サービス(Python パッケージ tanbo)
  config/parent.example.toml
  systemd/tanbo-parent.service, tanbo-web.service
  tests/                実 digi-xbee + XBee エミュレータでの結合テスト
gas/                    受信用 Apps Script(新スプレッドシートにバインド)
docs/PROTOCOL.md
```

## 全体の流れ

```
子機 ──REQ,<seq> / D2 応答── 親機 XBee ── poller ──▶ SQLite(/var/lib/tanbo) ── uploader ──POST──▶ GAS ──▶ data_YYYY-MM
                                               │                                               health_YYYY-MM / 最新 / 親機
                                               └─ 制御ソケット ── tanboctl(SSH)
                                                               └─ tanbo-web ◀── スマホ(Tailscale)
```

- 計測は壁時計に揃った周期(通常 10 分 → 毎時 00,10,20… 分)。設置チェック時だけ `tanboctl check` で一時的に 1 分周期にでき、期限が来ると自動で戻る
- 計測結果は必ず先に SQLite に書く。回線が切れても欠測にはならず、復旧後にまとめて送られる
- GAS は (db_uuid, 種別) ごとの高水位線で重複を弾く。再送しても二重記録にならない
- サイクルごとに親機自身の状態(健全性)も1行記録する。全子機が沈黙していても親機の生死が分かる
- 親機の操作は、親機上の操作画面(tanbo-web)をスマホから Tailscale 経由で開いて行う。GAS / スプレッドシートからは操作しない(GAS は受信専用)

## 1. GAS(先にやる)

書き込み先はスプレッドシート **tambo_data2** です(`Code.gs` の `SPREADSHEET_ID` に固定済み)。旧 `tanbo_data` は旧データとしてそのまま残します。

1. tambo_data2 を開き、拡張機能 → Apps Script。`Code.gs` を貼り、プロジェクトの設定で「appsscript.json を表示」にして `appsscript.json` も置き換える
2. エディタで `setup` を実行(権限を許可)。ログに出る `TOKEN = ...` を控える
   - タイムゾーンが Asia/Tokyo になり、「最新」「親機」「説明」シートと 10 分ごとの `checkStale` トリガーが作られる。空の「シート1」は削除される
   - 何度実行してもデータは消えない(「説明」シートだけ書き直す)
3. デプロイ → 新しいデプロイ → 種類「ウェブアプリ」、実行ユーザー「自分」、アクセス「全員」。URL を控える
4. 任意: スクリプトプロパティ `ALERT_EMAIL`(既定は自分)、`STALE_MIN`(既定 40 分)。書き込み先を別ファイルに変えるときは `SPREADSHEET_ID`

`Code.gs` を直したら「デプロイを管理 → 編集 → 新バージョン」で同じ URL のまま更新する。

### シート

| シート | 内容 |
|---|---|
| `data_YYYY-MM` | 計測(1ノード1行)。`Dist20_cm` は 20℃ 固定の名目距離。補正は `EchoMed_us` から後処理で |
| `health_YYYY-MM` | 親機の健全性(1サイクル1行) |
| `最新` | ノードごとの最新状態、最後に OK だった時刻と距離、連続失敗回数 |
| `親機` | 最新のハートビート |
| `説明` | 列と status の意味 |

容量の目安: 13 ノード × 10 分周期で約 0.8M セル/月。スプレッドシートの上限 10M セルに対して約 1 年分。

## 2. 親機

### インストール

```bash
sudo useradd -r -s /usr/sbin/nologin -G dialout,plugdev,i2c tanbo
sudo mkdir -p /opt/tanbo /etc/tanbo
sudo cp -r parent/* /opt/tanbo/
sudo python3 -m venv /opt/tanbo/venv
sudo /opt/tanbo/venv/bin/pip install /opt/tanbo
sudo cp /opt/tanbo/config/parent.example.toml /etc/tanbo/parent.toml
sudo chmod 640 /etc/tanbo/parent.toml && sudo chgrp tanbo /etc/tanbo/parent.toml
sudoedit /etc/tanbo/parent.toml      # port, url, token を設定
sudo ln -s /opt/tanbo/venv/bin/tanboctl /usr/local/bin/tanboctl
sudo cp /opt/tanbo/systemd/tanbo-parent.service /opt/tanbo/systemd/tanbo-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tanbo-parent tanbo-web
```

`port` は `ls -l /dev/serial/by-id/` で親機 XBee の FTDI を確認して設定する。`/dev/ttyUSB0` はモバイル通信モジュールと番号が入れ替わることがある。

旧サービス(旧 main.py 等)が同じシリアルポートを掴んでいると開けないので、先に止めて無効化しておく。

#### 親機の地点のセンサ(任意)

親機の設置場所の水位も測る場合は、ESP32 + HC-SR04 を親機に USB でつなぐ(XBee は不要)。

1. ESP32 に子機と同じファーム(`child/tanbo_child_v2`、v2.1.0 以降)を書く。HC-SR04 の配線は子機と同じ
2. `ls -l /dev/serial/by-id/` で ESP32 のポートを確認する(CP210x なら `usb-Silicon_Labs_CP210...`)
3. `/etc/tanbo/parent.toml` の `[local]` のコメントを外し、`port` と `node`(`[nodes]` と重ならない番号)を書いて再起動

シート・操作画面には、ほかの子機と同じ形でそのノード番号の行が増える。ESP32 が抜けている間は `PORT_ERR` を記録し、挿し直せば次の計測で戻る。

### 操作画面(スマホから)

親機と同じ tailnet に入ったスマホで `http://<親機の Tailscale 名>:8080/` を開く(MagicDNS が無効なら `http://100.x.y.z:8080/`)。ホーム画面に追加しておくと現地で開きやすい。

- 上部: 運用モード。「設置チェックを始める」で一時的に 1 分周期(30分〜6時間、期限が来ると自動で 10 分周期に戻る)。「全ノードを今すぐ計測」「未送信を今すぐ送る」
- ノード一覧: 最新の状態と距離。左のゲージはセンサー(上端)から水面までの距離を 0〜100 cm で示す。失敗中のノードは連続失敗回数、配送コード、最後の正常値(薄く表示)
- ノードをタップ: 1時間〜7日の推移グラフ(上ほど水位が高い。帯は1回の計測内のばらつき、下の赤い印は失敗)と「このノードに今すぐ問い合わせ」(記録には残らない。設置・調整中の確認用)
- 親機の状態: 直近の計測ごとの応答率、温度、ディスク、時計の同期、送信の状況
- 10 秒ごとに自動更新。計測サービスが止まっていても、記録済みのデータは表示される

アクセス制限: `[web] allow_interfaces`(既定 `lo`, `tailscale0`)に届いた接続しか受け付けないので、Wi-Fi・LTE・USB 通信端末の側からは開けない。tailnet に入れる端末は全部操作できるので、tailnet は自分の端末だけにしておく。

HTTPS にしたい場合は、`bind = "127.0.0.1"` にして `sudo tailscale serve --bg 8080` で `https://<親機>.<tailnet>.ts.net/` から開く。

### 操作(SSH から)

```bash
tanboctl status          # 周期、次回時刻、各ノードの最新値、未送信件数
tanboctl check           # 2時間だけ 60 秒周期(設置チェック用)。tanboctl check 30m など
tanboctl interval 120 1h # 任意の周期に一時変更
tanboctl normal          # 通常周期に戻す
tanboctl get 5           # ノード 5 に今すぐ1回問い合わせ(記録しない)
tanboctl poll            # 全ノードを今すぐ計測(記録する)
tanboctl flush           # 未送信を今すぐ送る
journalctl -u tanbo-parent -f
journalctl -u tanbo-web -f
```

`tanboctl` は `/run/tanbo/ctl.sock` を使うので、`sudo` か tanbo グループで実行する。周期の一時変更はサービスを再起動すると通常周期に戻る(戻し忘れ防止)。

### 計測 status

| status | 意味 |
|---|---|
| `OK` | 正常(v2 子機) |
| `NO_ECHO` | 子機は応答したが有効エコーが 0 回 |
| `OK_V1` / `NO_DATA` | 旧ファームの子機からの応答 |
| `TX_FAIL` | XBee の配送失敗。`TxStatus` にコード(シートでは 10 進)。0x24 (36) = 宛先が見つからない(子機の電源断・未参加。実機で確認)、0x21 (33) = 経路上で ACK なし、など |
| `TIMEOUT` | 配送は成功したが応答なし |
| `BAD_REPLY` | 解析できない応答(`Raw` に原文) |
| `RADIO_ERR` | 親機の XBee が使えない |
| `PORT_ERR` | 親機の地点のセンサ(USB の ESP32)が使えない(`Raw` に理由) |

### エラーの見方

```bash
journalctl -u tanbo-parent -p warning      # WARNING 以上だけ(ログレベルが journal の優先度に入る)
sudo tanboctl status                       # ノードごとの status / tx(配送コード)/ rtt、送信エラー
```

- サイクルごとのログに失敗の内訳が出る: `cycle 19:10:00: 1/11 OK in 18947 ms; TX_FAIL(0x24): 1,3-10,12`。
  内訳が前回から変わったサイクルだけ WARNING、同じ状態が続く間は INFO
- 配送は成功(`tx` = 0x00)したのに応答がない子機は `TIMEOUT`。子機本体(ESP32・電源・配線)を疑う
- 全ノードが `TX_FAIL` のときは健全性の `XBeeAI` を見る。0 以外なら親機 XBee がネットワークを作れていない
- `tanboctl status` の「最終送信成功」は、最後に GAS が受理した時刻(送るものがなかった時刻は含まない)

送信エラー(journal、`tanboctl status`、シートの `UploadErr`)は「分類: 原因」の形:

| 分類 | 意味 |
|---|---|
| `NET` | 回線・DNS・接続先。相手に届いていない(例 `NET: ConnectionRefusedError: [Errno 111] Connection refused`) |
| `TIMEOUT` | 接続(`connect`)または応答(`read`)の時間切れ |
| `HTTP` | HTTP ステータスが 4xx / 5xx(URL 違い、デプロイ削除など) |
| `RESP` | 想定外の応答。JSON でない(ウェブアプリのアクセスが「全員」でないとログイン画面が返る)、`hwm` がない(doPost が実行されず doGet の応答が返った)など。括弧内はたどったリダイレクトの経路 |
| `GAS` | GAS が受理しなかった(`GAS: bad token` など) |
| `ERR` | その他 |

### 障害時の自己回復

- サービスが落ちたら 20 秒後に再起動(回数制限なし)
- 計測ループか送信ループが 5 分止まると systemd のウォッチドッグが再起動
- XBee が開けない状態が 30 分続くとプロセスを終了して再起動。ただし XBee が抜けている(`port` のパスがない)ときは再起動しても直らないので、再起動せず 30 秒ごとに開き直しを試みる(挿せば自動で再開)
- GAS へのリダイレクトは自前でたどり、結果ページ(`/macros/echo`)以外への転送は POST のまま送り直す(requests 任せだと GET に変わって doGet が走る)
- 送信失敗は 30 秒〜15 分の指数バックオフで再送
- 起動直後に NTP 同期で時計が大きく進んだ場合、古い時刻の枠では計測しない。各行に `ClockSynced` と `boot_id` を残すので、後から時刻を検証できる

### OS 側(推奨)

ハードウェアウォッチドッグ(OS ごと固まった場合の保険):

```ini
# /etc/systemd/system.conf
RuntimeWatchdogSec=15
```

SD カード保護: `raspi-config` の overlayfs でルートを読み取り専用にする場合、`/var/lib/tanbo` は書き込み可能な別パーティションに置く。そうしないと未送信バッファが再起動で消える。

## 3. 追加ハード(買い足す場合)

- **RTC(DS3231)**: 電源喪失からの復帰直後、回線が上がる前の時刻を保証する。コード変更は不要。`/boot/firmware/config.txt` に `dtoverlay=i2c-rtc,ds3231` を追加し、`fake-hwclock` を外す
- **電圧計測(INA219)**: 鉛蓄電池の電圧を健全性に記録する。VIN- 側をバッテリー + に繋ぐ(バス電圧は最大 26V)。設定の `ina219_bus` / `ina219_addr` を有効にする
- **電波強度**: モバイル通信モジュールが ModemManager 管理下なら `modem = "mmcli"`

## 4. 子機

`child/tanbo_child_v2/tanbo_child_v2.ino` を全子機に書き込む。配線と XBee 設定は旧子機と同じ(AP=0、DH/DL=0、BD=9600)。

- REQ を受けた瞬間に 60ms 間隔で 7 回測り、有効エコー時間の中央値・最小・最大を返す
- 約 5cm 未満(290µs)と約 2m 超(11650µs)のエコーは捨てる。Node_5 の飛びの解析用に min/max も記録する
- 親機からの REQ が 6 時間途絶えると ESP32 を再起動する
- 旧ファームの子機が混在していても親機は受け付ける(`OK_V1`)。順次入れ替えてよい
- USB シリアル(115200 bps)から届いた REQ には USB に返す(v2.1.0 以降)。同じファームを親機の地点のセンサにも使う

## 5. テスト

```bash
cd parent && python3 -m pytest -q tests     # 実 digi-xbee + pty XBee エミュレータ + 偽 GAS + 操作画面 API
python3 parent/tests/demo_web.py 8099       # 合成データで操作画面を起動(見た目の確認用)
node gas/test_gas.js                        # Code.gs を Apps Script モック上で
```

子機は C++ のモック環境で、バースト測定、中央値、ブランキング、改行なし REQ、REQ 途絶時の再起動を確認済み。いずれも実機では未確認。
