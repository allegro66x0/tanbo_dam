# Pi 作業ログ

Pi 上の Claude Code が調査結果・作業内容・判断を日付付きで追記する。

## 2026-10-01 Phase 0: 現状調査(変更なし)

読み取りコマンドだけを実行した。サービスの停止・`/etc` の変更・再起動などは一切していない。
`sudo` はパスワードが必要で、この作業では使っていない(root の crontab など一部は未確認)。

### 要点(先に読む)

1. **この Pi は本番の親機ではなく、旧「子機 Node 3」として設定されている可能性が高い。**
   動いている旧プログラム `/home/p3/xbee_sensor/main.py` は `NODE_ID = "3"` の子機(Router)で、
   `REQ` を受けて `ID3,<dist>,<temp>,<hum>,<pres>` を返す側。GAS へ送るコードも、他ノードへ `REQ` を出すコードもない。
   HANDOFF が想定する「旧親機プログラム」はこの Pi 上に見つからなかった。
2. **XBee もモバイル通信モジュールも接続されていない。** USB にはキーボード(Keyball44)だけ。
   `/dev/ttyUSB*`・`/dev/serial/by-id/` は存在しない。今回の起動(15:57〜)のカーネルログにも FTDI やモデムの接続記録はない。
3. **旧サービス `xbee_sensor.service` は約 5 秒ごとに再起動を繰り返している**(調査時点で 631 回)。
   `/dev/ttyUSB0` が開けずに終了 → `Restart=always` で再起動、の繰り返し。実害は journal への書き込みのみ。
4. Python 3.13.5 で v2 の要件(3.11 以上)は満たす。Phase 1(テスト)はハードなしで進められる。
   Phase 2 以降は XBee を挿すまで進められない。

### OS / Python

| 項目 | 値 |
|---|---|
| 機種 | Raspberry Pi 4 Model B Rev 1.5、メモリ 8GB |
| OS | Debian GNU/Linux 13 (trixie) 13.4、64bit (aarch64) |
| カーネル | 6.12.75+rpt-rpi-v8 |
| ホスト名 / ユーザー | `p3` / `p3`(uid 1000)。`/home/pi0` は存在しない |
| Python | 3.13.5(`/usr/bin/python3`)。`tomllib`・`venv` あり、SQLite 3.46.1 |
| pip | 25.1.1(システム)。`python3-venv` 導入済み |
| システムに入っている関連パッケージ | digi-xbee 1.5.0、pyserial 3.5、requests 2.32.3、smbus2 0.4.3。pytest はなし |
| node | なし(`node gas/test_gas.js` はこの Pi では実行できない) |
| 温度 / 電圧低下 | 51.6℃、`throttled=0x0` |

デスクトップ環境(lightdm、wayvnc、cups、bluetooth など)が有効なイメージ。常駐運用には不要なものが多い。

### 旧プログラム

| ユニット | 状態 | 内容 |
|---|---|---|
| `xbee_sensor.service` | enabled、再起動ループ中 | `ExecStart=/usr/bin/python3 /home/p3/xbee_sensor/main.py`、`User=p3`、`Restart=always` |
| `xbee_node.service` | enabled、failed | `/home/pi0/xbee_sensor/main.py`、`User=pi0`。ユーザー pi0 がいないので 217/USER で失敗。残骸 |

- どちらも `/etc/systemd/system/` に直接置かれている(2026-05-21 作成)
- p3 の crontab なし、`/etc/rc.local` なし、`/etc/cron.d` は標準のものだけ。root の crontab は sudo が要るので未確認
- `/home/p3/xbee_sensor/main.py`(2026-08-26 更新): 子機 Node 3。VL53L1X(0x29)+ BME280(0x76)を I2C で読み、
  `PORT = "/dev/ttyUSB0"`、9600bps。`msg == "REQ"` の完全一致で応答する
- `/home/p3/xbee_sensor/test.py`: `NODE_ID = "1"` の別版(VL53L0X)
- `/home/p3/tambokoki/xbee_sensor/main.py`(2026-08-25): 同じ子機 Node 3 の別コピー。
  `PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL01T2NS-if00-port0"` と書かれている
  → 過去にこの Pi に挿していた XBee アダプタは FTDI FT232R(シリアル AL01T2NS)
- `/home/p3/tambokoki/xbee_sensor/ch/main.py`: 「XBee3 Relay Mode (Router3)」。コーディネータへの中継スクリプト

**v2 との互換性の注意**: この旧 Pi 子機は `msg == "REQ"` の完全一致なので、v2 親機の `REQ,<seq>\n` には応答しない。
PROTOCOL.md の「旧子機は `indexOf("REQ")` で反応する」は ESP32 版の旧子機の話で、この Pi 版子機には当てはまらない。
Pi 版子機が現場にまだ残っているなら `TIMEOUT` になる。

### シリアル / XBee

- `/dev/serial/by-id/` なし、`/dev/ttyUSB*`・`/dev/ttyACM*` なし
- あるのは `/dev/ttyS0`(`/dev/serial0`、GPIO の mini UART。`enable_uart=1`)だけ。掴んでいるプロセスなし
- `lsusb`: ルートハブ、VIA Labs ハブ、Keyball44(キーボード)のみ

### モバイル通信 / ネットワーク

- モデムは未接続。`mmcli` は未インストール、ModemManager は inactive
- 現在の回線は Wi-Fi(`wlan0`、172.25.15.65/16、既定経路 172.25.0.254)。`eth0` はリンクなし
- NetworkManager 管理。保存済み接続は Wi-Fi 4件と有線 1件(名前は記録しない)。モバイル(gsm)の接続設定はない

### 時刻同期

- `systemd-timesyncd` が active、`System clock synchronized: yes`、タイムゾーン Asia/Tokyo。chrony はなし
- `RTC time: n/a`(RTC なし)。電源喪失後は回線が上がるまで時刻が不定になる

### I2C

- `/dev/i2c-1` あり(ほかに i2c-20、i2c-21)。`i2cdetect -y 1` は**デバイス 0 個**
- `/boot/firmware/config.txt` は `dtoverlay=i2c-gpio,bus=1,i2c_gpio_sda=2,i2c_gpio_scl=3,i2c_gpio_delay_us=50`
  (ハードウェア I2C ではなくソフトウェア I2C)。旧子機センサー用の設定と思われる
- INA219 や RTC は現状つながっていない

### ストレージ

- microSD 29.5GB: `mmcblk0p1` 512M vfat `/boot/firmware`、`mmcblk0p2` 29G ext4 `/`(使用 7.4G / 27%)
- パーティションは 2 つだけ。**`/var/lib/tanbo` 用の別パーティションはない**(Phase 4 で overlayfs を使うなら分割が必要)
- overlayfs: 無効(`get_overlay_now` = 1、`get_overlay_conf` = 1)。`/` は rw,noatime。boot も読み書き可
- スワップは zram 2G
- journal は 8M、起動記録は今回の 1 回分だけ(`/var/log/journal` がなく揮発。再起動前のログは残らない)

### ユーザーとグループ

- `dialout`(gid 20)と `i2c`(gid 988)は存在し、どちらも `p3` が所属。`gpio`・`spi` も同様
- `tanbo` ユーザーは未作成(Phase 3 で作る)

### ウォッチドッグ

- `/dev/watchdog`・`/dev/watchdog0` あり
- `systemctl show` では `RuntimeWatchdogUSec=1min`。`/etc/systemd/system.conf` の該当行はコメントのまま
  (どこで 1 分が設定されているかは未確認。Phase 4 で確認する)

### 判断に迷ったこと / ユーザーに確認したいこと

1. **この Pi(p3)を v2 の本番親機にしてよいか。** 旧設定は子機 Node 3。本番親機が別の Pi(pi0 など)なら、
   そちらで作業すべき。この Pi を親機に転用するなら、現場の Node 3 がどうなるかも確認が必要
2. **旧親機プログラムのソースはどこにあるか。** この Pi にはない。GAS の URL・ノードの MAC アドレス一覧など、
   `[nodes]` 設定に必要な情報は旧親機か XCTU から取る必要がある
3. **親機用 XBee(AP=1、CE=1 のコーディネータ)とモデムはいつ挿せるか。** 過去の FT232R(AL01T2NS)は
   子機(Router)設定のはずで、そのままでは親機として使えない
4. `xbee_sensor.service` の再起動ループを止めるか(止める・無効化するには sudo とユーザーの許可が必要)。
   `xbee_node.service` は pi0 用の残骸で、無効化してよさそうに見える

### 次の作業

- Phase 1(venv を作ってテスト)は `~/tanbo_dam/parent/.venv` の作成と pip のダウンロードだけで、ハードなしで実行できる
- Phase 2 は XBee 接続と上記 1〜3 の回答待ち

## 2026-10-01 ユーザーからの回答

- **親機はこの個体(p3)で確定。** 旧子機 Node 3 の設定は過去のもの
- ノードの 64bit アドレスは 0〜12 の 13 個。`parent/config/parent.example.toml` の `[nodes]` と全件一致することを確認した
- 親機用 XBee を挿してもらった。子機は **Node 2 だけ**が通信範囲内で電源が入っている(ほかの 12 台は `TX_FAIL` が正常)
- Node 2 は不調の可能性あり。**配送成功(Transmit Status = 成功)なのにデータが来ない場合は子機側の不調**と扱う(`TIMEOUT`)

## 2026-10-01 Phase 1: テスト

```
cd parent && python3 -m venv .venv && . .venv/bin/activate
pip install -e . pytest      # digi-xbee 1.5.0 / pyserial 3.5 / requests 2.34.2 / pytest 9.1.1
python -m pytest -q tests    # 7 passed in 37.75s
```

- Python 3.13.5 で 7 件すべて成功。時間依存のアサーションでの失敗なし。テスト側の変更なし
- pip のダウンロード中に接続切れのリトライが 1 回出たが、インストールは完了した

## 2026-10-01 Phase 2: 準備(旧サービス停止待ち)

- 親機 XBee: `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AQ04PSC2-if00-port0` → `ttyUSB0`(FTDI FT232R、0403:6001)
  - example の `port`(`...AQ04QLF1...`)とはシリアルが違う。本番の `parent.toml` では `AQ04PSC2` にする
- **旧 `xbee_sensor.service` が XBee を挿した直後に `/dev/ttyUSB0` を掴んだ**(17:11:59 起動の PID 6973、以後再起動なし)。
  `XBeeDevice.open()` が通っているので、この XBee は API モードで応答していると推定できる
- このままでは v2 を起動できない。停止には sudo が必要で、ユーザーに
  `sudo systemctl stop xbee_sensor.service` を依頼中(無効化は Phase 3 まで行わない)
- 一時設定 `~/tanbo_test.toml` を作成済み(リポジトリ外): `port` = 上記 by-id、`interval_s = 60`、
  `db_path = ~/tanbo_test.db`、`control.socket = ~/tanbo_test.sock`、`upload.url = http://127.0.0.1:9/dummy`(どこにも送らない)

## 2026-10-01 Phase 2: 実機 XBee での疎通

ユーザーは Pi の手元を離れている。「この Pi では何をしてもよい」との許可と sudo パスワードをもらい、こちらで作業した。

### やったこと

1. `sudo systemctl stop xbee_sensor.service`(17:53 頃)。**停止だけで、無効化はしていない**。
   再起動すると旧サービスがまた起動して `/dev/ttyUSB0` を掴む(Phase 3 で無効化する)
2. `python -m tanbo -c ~/tanbo_test.toml -v` を起動。`XBee open: ... MAC=0013A200423EB740` が出た
3. `tanbo.ctl status` / `get 2` / `get 5` / `poll`、60 秒周期で 3 サイクル + 修正後に 1 サイクル
4. テスト起動は終了済み。ポートを掴んでいるプロセスはない。v2 はまだ常駐していない

### 親機 XBee の設定(AT コマンドで読み出し)

| 項目 | 値 |
|---|---|
| 機種 / FW | XBee3 TH、Zigbee、VR=100D |
| 役割 | コーディネータ(CE=1)、AP=1、MY=0000、AI=00(ネットワーク形成済み) |
| 64bit アドレス | **0013A200423EB740** |
| PAN ID | ID=1114、OP=1114、OI=062D、CH=0x18 |
| その他 | NJ=FE、SM=0、PL=4、EE=0(暗号化なし)、AO=0、供給電圧 %V=3327mV、温度 TP=36℃ |

ネットワーク探索で見つかったのは Node 2 だけ: `0013A200423EC0CD`、16bit=29E4、NI=`cyuukei1`、Router。

### 結果

| ノード | status | tx_status | 所要時間 |
|---|---|---|---|
| 2 | `OK_V1`(毎回) | 0x00 | 応答まで 138〜154 ms |
| 0,1,3〜10,12 | `TX_FAIL` | **0x24(Address not found)** | 1 台あたり約 1.85 秒 |
| 11 | `BAD_REPLY` → 修正後はスキップ | なし | 71 ms(自分宛ての折り返し) |

- Node 2 の距離: 77.1 / 77.1 / 77.6 / 77.0 / 72.1 / 74.6 / 75.8 / 75.8 / 76.2 / 76.2 / 76.2 cm(計 11 回、欠測なし)。
  旧ファーム(v1)なので `seq` なし、`fw = v1`。**子機 2 は正常に応答している**(配送成功でデータなし、は起きなかった)
- `cycle_ms`: 13 台で 21089 / 21076 / 21078 ms、修正後 12 台で 20809 ms。60 秒周期でも余裕がある
- 健全性行: `synced=1`、`xbee_ai=0`、`cpu_temp` 約 45℃、`batt_v`・`modem_sig` は None(未接続)
- 送信はダミー URL なので失敗し、30 → 60 → 120 秒とバックオフした。未送信は SQLite に溜まった(計測 59 / 健全性 5)

### 実機で分かった差と修正

1. **一覧のラベル 11(0013A200423EB740)は、いま挿さっている親機 XBee 自身のアドレス。**
   自分宛てに `REQ,<seq>` を送ると、そのまま受信フレームとして折り返ってきて `BAD_REPLY`(raw = `REQ,8141`)になった。
   - 修正: `poller.run_cycle` で `mac == radio.local_mac` のノードを飛ばし、初回だけ WARNING を出す
   - エミュレータ(`xbee_emu.py`)にも自分宛ての折り返しを追加し、`test_own_address_is_skipped` を追加
   - プロトコルと GAS の列は変えていない(該当ノードの行が出なくなるだけ)
   - **未解決**: 子機 11 が別にあるなら正しいアドレスが必要。ユーザーに確認中(下記)
2. **届かないノードの Transmit Status は 0x24(Address not found)で、返ってくるまで約 1.85 秒。**
   エミュレータは 0x21 を 0.3 秒で返す。README の例「0x21 = 経路上で ACK なし」も実機では別のコードだった。
   動作に支障はないのでエミュレータは変えていない(電源が入っていて圏外のノードは 0x21 になる可能性がある)
3. digi-xbee 1.5.0 の挙動(オープン、非同期送信、Transmit Status、受信)はエミュレータと同じで、コード修正は不要だった

### テスト

`python -m pytest -q tests` → 8 passed in 41.21s(追加 1 件を含む)

### 残っているファイル(リポジトリ外)

`~/tanbo_test.toml`、`~/tanbo_test.db`(計測 59 行)、`~/tanbo_test.log`、`~/tanbo_test2.log`。Phase 3 が済んだら消してよい。

### ユーザーに確認したいこと

1. **ラベル 11 の扱い**: 親機 XBee が 11 番のアドレスだった。(a) 一覧の 11 は親機のことで子機は 12 台、
   (b) 子機 11 の XBee を親機に転用した、(c) 別のコーディネータ用 XBee を挿すつもりだった、のどれか
2. **GAS(tambo_data2)のデプロイは済んでいるか。** Phase 3 では `/etc/tanbo/parent.toml` に `url` と `token` が必要。
   HANDOFF ではユーザー自身が書くことになっているが、ユーザーは Pi の手元にいない
3. Phase 3 で旧サービス 2 つ(`xbee_sensor`、`xbee_node`)を無効化してよいか(許可はもらっているが、戻し方を含め念のため)
