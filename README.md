# 田んぼダム 水位計測システム v2

親機(Raspberry Pi 4)・GAS・子機(ESP32)を作り直した版です。プロトコルは `docs/PROTOCOL.md` にあります。

```
child/tanbo_child_v2/   子機ファームウェア(全子機同一)
parent/                 親機サービス(Python パッケージ tanbo)
  config/parent.example.toml
  systemd/tanbo-parent.service
  tests/                実 digi-xbee + XBee エミュレータでの結合テスト
gas/                    受信用 Apps Script(新スプレッドシートにバインド)
docs/PROTOCOL.md
```

## 全体の流れ

```
子機 ──REQ,<seq> / D2 応答── 親機 XBee ── poller ──▶ SQLite(/var/lib/tanbo) ── uploader ──POST──▶ GAS ──▶ data_YYYY-MM
                                               │                                               health_YYYY-MM / 最新 / 親機
                                               └─ tanboctl(unix ソケット)
```

- 計測は壁時計に揃った周期(通常 10 分 → 毎時 00,10,20… 分)。設置チェック時だけ `tanboctl check` で一時的に 1 分周期にでき、期限が来ると自動で戻る
- 計測結果は必ず先に SQLite に書く。回線が切れても欠測にはならず、復旧後にまとめて送られる
- GAS は (db_uuid, 種別) ごとの高水位線で重複を弾く。再送しても二重記録にならない
- サイクルごとに親機自身の状態(健全性)も1行記録する。全子機が沈黙していても親機の生死が分かる

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
sudo cp /opt/tanbo/systemd/tanbo-parent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tanbo-parent
```

`port` は `ls -l /dev/serial/by-id/` で親機 XBee の FTDI を確認して設定する。`/dev/ttyUSB0` はモバイル通信モジュールと番号が入れ替わることがある。

旧サービス(旧 main.py 等)が同じシリアルポートを掴んでいると開けないので、先に止めて無効化しておく。

### 操作

```bash
tanboctl status          # 周期、次回時刻、各ノードの最新値、未送信件数
tanboctl check           # 2時間だけ 60 秒周期(設置チェック用)。tanboctl check 30m など
tanboctl interval 120 1h # 任意の周期に一時変更
tanboctl normal          # 通常周期に戻す
tanboctl get 5           # ノード 5 に今すぐ1回問い合わせ(記録しない)
tanboctl poll            # 全ノードを今すぐ計測(記録する)
tanboctl flush           # 未送信を今すぐ送る
journalctl -u tanbo-parent -f
```

`tanboctl` は `/run/tanbo/ctl.sock` を使うので、`sudo` か tanbo グループで実行する。周期の一時変更はサービスを再起動すると通常周期に戻る(戻し忘れ防止)。

### 計測 status

| status | 意味 |
|---|---|
| `OK` | 正常(v2 子機) |
| `NO_ECHO` | 子機は応答したが有効エコーが 0 回 |
| `OK_V1` / `NO_DATA` | 旧ファームの子機からの応答 |
| `TX_FAIL` | XBee の配送失敗。`TxStatus` にコード(0x21 = 経路上で ACK なし、など) |
| `TIMEOUT` | 配送は成功したが応答なし |
| `BAD_REPLY` | 解析できない応答(`Raw` に原文) |
| `RADIO_ERR` | 親機の XBee が使えない |

### 障害時の自己回復

- サービスが落ちたら 20 秒後に再起動(回数制限なし)
- 計測ループか送信ループが 5 分止まると systemd のウォッチドッグが再起動
- XBee が開けない状態が 30 分続くとプロセスを終了して再起動
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

## 5. テスト

```bash
cd parent && python3 -m pytest -q tests     # 実 digi-xbee + pty XBee エミュレータ + 偽 GAS
node gas/test_gas.js                        # Code.gs を Apps Script モック上で
```

子機は C++ のモック環境で、バースト測定、中央値、ブランキング、改行なし REQ、REQ 途絶時の再起動を確認済み。いずれも実機では未確認。
