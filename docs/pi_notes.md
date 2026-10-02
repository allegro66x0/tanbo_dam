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

## 2026-10-01 ユーザーからの回答(2)

- アドレス一覧は「研究室にあった XBee すべて」のメモ。**0 番は現地で稼働中の旧親機、11 番はこの Pi の v2 親機**に使った。
  子機は 1〜10 と 12 の **11 台**
- この Pi はいずれ現地の親機を置き換える予定だが、**当面は研究室に置く**(モバイル通信のテストがまだ)
- GAS(tambo_data2)のデプロイは**まだ**。`url` と `token` はユーザーが自分で書く(会話に出さない)方針で合意
- この Pi で動かすプログラムは v2 に入れ替えてよい、との指示

## 2026-10-01 Phase 3: サービス化(url / token 未設定のまま常駐)

### やったこと

1. 旧サービスを無効化: `sudo systemctl disable --now xbee_sensor.service xbee_node.service`
   - ユニットファイル(`/etc/systemd/system/xbee_*.service`)と旧コード(`~/xbee_sensor`、`~/tambokoki`)は残してある
   - 戻し方: `sudo systemctl disable --now tanbo-parent && sudo systemctl enable --now xbee_sensor.service`
2. README の手順で導入: ユーザー `tanbo`、`/opt/tanbo`(venv 込み)、`/etc/tanbo/parent.toml`(640 root:tanbo)、
   `/usr/local/bin/tanboctl`、`/etc/systemd/system/tanbo-parent.service`、`enable --now`
3. `/etc/tanbo/parent.toml` の内容: `port` は `...AQ04PSC2...`、`[nodes]` は 1〜10 と 12 の 11 台、`interval_s = 600`。
   **`url` は `http://127.0.0.1:9/unset`(どこにも送らない仮の値)、`token` は example の文言のまま**
4. `~/tanbo_test.*`(Phase 2 の一時ファイル)は削除した

### README との差(修正済み)

- **`/dev/ttyUSB0` のグループが `dialout` ではなく `plugdev` だった。** FTDI(0403:6001)に対して
  `60-openocd.rules` / `60-flashrom.rules` が `GROUP="plugdev"` を付けるため(デスクトップ版イメージに入っている)。
  `tanbo` ユーザーを `dialout,plugdev,i2c` に入れ、ユニットの `SupplementaryGroups` と README の `useradd` にも `plugdev` を足した
- `parent.example.toml`: `port` をこの Pi の XBee に合わせ、`[nodes]` から 0 と 11 を外して理由をコメントした

### 確認できたこと

- `systemctl status tanbo-parent` が active、journal にエラーなし(送信失敗の WARNING は `url` 未設定のため想定どおり)
- 最初のサイクルは **18:10:00**(10 分境界)。`1/11 OK in 18944 ms`、Node 2 は `OK_V1` 74.6 cm、ほかは `TX_FAIL`
- `tanboctl status`(`sudo` で実行)で次回 18:20:00、未送信 計測 11 / 健全性 1
- **ウォッチドッグ**: 18:10:30 に `systemctl kill -s SIGSTOP` → 18:15:28 に systemd が SIGABRT で落とし(`Failed with result 'watchdog'`)、
  20 秒後の 18:15:48 に再起動。再起動後も未送信 11 / 1 は SQLite に残っていた
- 再起動後も XBee を開き直せた

### 未確認 / 残り

- **GAS への送信**: `url` / `token` 未設定のため未確認。未送信は `/var/lib/tanbo/tanbo.db` に溜まり続ける
  (11 台 × 10 分で 1 日約 1600 行。`retain_days = 365`)。設定後にまとめて送られるはず
- ユーザーが研究室でやること:
  1. README「1. GAS」で tambo_data2 にデプロイし、URL と TOKEN を控える
  2. `sudoedit /etc/tanbo/parent.toml` で `url` と `token` を書く
  3. `sudo systemctl restart tanbo-parent`、`sudo tanboctl status` で未送信が減ること、シートに行が増えることを確認
- `tanboctl status` の「最終送信」は、送信失敗中でも時刻が出ることがあった(18:10:18 と表示、実際は未送信 11 件のまま)。
  表示の意味(最終成功か最終試行か)は未調査
- この Pi の再起動後に自動で立ち上がるかは未確認(再起動していない)
- モバイル通信は未テスト(モデム未接続、Wi-Fi で運用中)。Phase 4(堅牢化)は未着手
- GitHub への push は認証がなくできていない。コミットは Pi のローカル `main` にある

## 2026-10-01 エラーの出方の現状確認と、パターンの整理

コードは変更していない。「実機で確認」と書いたもの以外は、コードとテストから読み取った想定。

### エラーが出る場所(現状)

| 場所 | 見えるもの | 見方 |
|---|---|---|
| 計測行(SQLite → `data_YYYY-MM`) | ノードごとの `status`、`tx_status`、`rtt_ms`、`raw` | シート / `sudo tanboctl status` / `tanboctl get <n>` |
| 健全性行(→ `health_YYYY-MM`、`親機`) | `n_ok/n_nodes`、`cycle_ms`、`queue_m/h`、`xbee_ai`、`upload_err`、`synced`、`boot_id` | シート |
| journal | 親機プロセスの WARNING / ERROR / CRITICAL、systemd の再起動記録 | `journalctl -u tanbo-parent` |
| `tanboctl` 自身 | 権限なし、サービス停止、設定にないノード | 標準エラー、終了コード 1(実機で確認) |
| GAS のメール | 親機のハートビートが 40 分途絶 / 復旧 | `checkStale` |

### 現状の出方で気づいた点(実機で確認)

1. **journal の優先度がすべて 6(info)**。レベルは本文の文字列にしかなく、`journalctl -p warning` では絞れない。
   `journalctl -u tanbo-parent | grep -E "WARNING|ERROR|CRITICAL"` が必要
2. **送信エラーの文面が 200 文字で切れ、肝心の原因が落ちる**。いまの文面は
   `ConnectionError: HTTPConnectionPool(host=..., port=9): Max retries exceeded with url: ... Failed to establish a new ` で終わり、
   末尾の「Connection refused」などが見えない。journal・`tanboctl status`・`health.upload_err` のどれも同じ
3. **`tanboctl status` の「最終送信」は最後に POST が成功した時刻ではない**。送る行が 0 件でも「成功」として時刻が入る
   (`uploader.last_ok`)。未送信が残っているのに時刻が出ることがある
4. **ノードごとの失敗は journal に出ない**。出るのは `cycle 19:10:00: 1/11 OK in 18947 ms` だけ(`BAD_REPLY` のみ WARNING が出る)。
   どのノードがなぜ失敗したかは DB / シート / `tanboctl status` を見る必要がある
5. **`tanboctl status` の表に `tx_status` と `rtt_ms` がない**。`TX_FAIL` の種類は `tanboctl get <n>` かシートでしか分からない
6. **README と GAS「説明」シートの例は 0x21 だが、実機で出たのは 0x24(10 進 36)**。シートには 10 進で入る
7. 子機ごとの異常を知らせる通知はない(メールは親機の途絶だけ)。`最新` シートの「連続失敗」を見に行く必要がある

### パターンと識別方法

子機・無線側(1 計測行で識別):

| 原因 | status | tx_status | そのほかの手がかり |
|---|---|---|---|
| 子機の電源断・ネットワーク未参加 | `TX_FAIL` | 0x24 (36) | 約 1.85 秒で返る。**実機で確認** |
| 参加済みだが圏外・経路切れ | `TX_FAIL` | 0x21 (33) / 0x25 (37) の想定 | 未確認。同じノードで 0x24 と区別できるはず |
| 電波の混雑 | `TX_FAIL` | 0x01 / 0x02 の想定 | 未確認 |
| XBee は生きているが ESP32 が応答しない(固まり、配線、電源) | `TIMEOUT` | 0 | `reply_timeout_s`(3 秒)待つので `cycle_ms` が伸びる |
| センサーがエコーを取れない | `NO_ECHO`(v2)/ `NO_DATA`(v1) | 0 | v2 は `n_ok=0` |
| 測定が不安定(エラーではない) | `OK` | 0 | `Valid/Tries` が低い、`EchoMin`〜`EchoMax` の幅が広い |
| 応答の文字化け・途中切れ | `BAD_REPLY` | 0 | `Raw` に原文、journal に `node N bad reply:` |
| 応答が遅れて次の番に届く | その回は `TIMEOUT` | 0 | journal に INFO `late reply` / `out-of-turn` / `stale`(v1 は seq がないので遅延と判定できない) |
| 子機の再起動 | `OK` | 0 | `ChildUptime_s` が前回より小さい(v2 のみ) |
| 一覧にない XBee からの受信 | 行なし | - | journal に WARNING `frame from unknown MAC`(1 時間に 1 回まで) |

親機 XBee 側(全ノードが同じ status になる):

| 原因 | 見え方 |
|---|---|
| XBee が抜けた・ポートが開けない・権限なし | 全ノード `RADIO_ERR`、`XBeeAI` 空、journal に ERROR `XBee open failed:` / `send to node N failed:` / `XBee ping failed:`、`tanboctl status` が `XBee NG`。30 分続くと CRITICAL `fatal: XBee unavailable` で再起動 |
| AP=1 でない | 上と同じで、文面が `コーディネータは AP=1 が必要` |
| ネットワーク未形成 | 全ノード `TX_FAIL`、`XBeeAI` が 0 以外 |
| 全子機が本当に不在 | 全ノード `TX_FAIL`、`XBeeAI = 0`(上と `XBeeAI` で区別) |

送信側(`upload_err` の先頭の例外名と `queue_m` の増加で識別):

| 原因 | `upload_err` の出方 |
|---|---|
| 回線断・DNS 不可・URL 未設定 | `ConnectionError: ...`(**実機で確認**。DNS なら文中に `NameResolutionError` の想定) |
| 回線が遅い | `ReadTimeout` / `ConnectTimeout` |
| URL 違い・デプロイ削除 | `HTTPError: 404 ...` など |
| ウェブアプリのアクセスが「全員」でない | `RuntimeError: non-JSON response: '<!DOCTYPE html...'`(ログイン画面が返る想定) |
| トークン違い | `RuntimeError: GAS error: bad token`(テストで確認) |
| GAS 側の例外・ロック待ち・割り当て超過 | `RuntimeError: GAS error: <GAS のメッセージ>` / `lock timeout` |

送信が失敗している間は `upload_err` 自体がシートに届かない。その間に外から分かるのは GAS のメール(40 分途絶)だけ。

親機本体(journal と健全性行の途切れ方で識別):

| 原因 | 見え方 |
|---|---|
| プロセスが固まった | journal `Failed with result 'watchdog'` → 20 秒後に再起動(**実機で確認**) |
| プロセスが落ちた | journal にトレースバック、`systemctl show -p NRestarts` が増える。`boot_id` は変わらない |
| 電源断・OS 再起動 | `BootId` が変わる、`Uptime_h` が小さくなる、健全性行が飛ぶ |
| 設定ファイルの誤り | 起動のたびに `ConfigError` で落ち、20 秒ごとに再起動を繰り返す。行は増えない |
| 計測が周期に間に合わない | journal WARNING `cycle overran; skipped slot(s)`、サイクルが抜ける |
| 時計が未同期・大きく進んだ | `ClockSynced = 0`、journal WARNING `clock jumped forward` |
| ディスク満杯・DB 書き込み失敗 | journal に `cycle failed` とトレースバック、`DiskFree_MB` が小さい |

### 直すとよさそうな点(未着手。ユーザーと相談)

- ログレベルを journal の優先度に反映する(`<3>` などの接頭辞)→ `journalctl -p warning` で絞れる
- 送信エラーを「分類 + 末尾の原因」に整える(例 `NET: Connection refused`)。200 文字切りで原因が落ちないようにする
- 「最終送信」を最後に POST が成功した時刻にし、「最終試行」と分ける
- サイクルのログに失敗ノードの内訳を 1 行で出す(例 `TX_FAIL(0x24): 1,3-10,12`)。状態が変わったときだけ出す形でもよい
- `tanboctl status` の表に `tx` と `rtt` の列を足す
- README と GAS「説明」の TxStatus の例に 0x24(36)を足す

## 2026-10-01 エラー表示の改善(実装・導入済み)

前節「直すとよさそうな点」の 6 項目を実装し、`/opt/tanbo` に入れ直してサービスを再起動した(19:43)。
プロトコルと GAS に送る列は変えていない(`UploadErr` 列の文面の形式だけ変わる)。

| 項目 | 変更 | 実機での確認 |
|---|---|---|
| 送信エラーの文面 | `uploader.describe_error` で「分類: 原因」に整形(NET / TIMEOUT / HTTP / RESP / GAS / ERR) | `NET: ConnectionRefusedError: [Errno 111] Connection refused` |
| 失敗ノードの内訳 | サイクルのログに追記。内訳が変わったサイクルだけ WARNING | `cycle 19:44:06: 1/11 OK in 18961 ms; TX_FAIL(0x24): 1,3-10,12` |
| journal の優先度 | systemd 配下では行頭に `<N>` を付ける(`JournalFormatter`) | `journalctl -u tanbo-parent -p warning` で WARNING だけ出た |
| 最終送信 | `last_ok` は GAS が受理した POST の時刻だけ。`error_since`(連続失敗の開始時刻)を追加 | `最終送信成功 -`、`送信エラー(10-01 19:43:20〜): ...` |
| `tanboctl status` の表 | `tx`(16 進)と `rtt[ms]` の列を追加 | `TX_FAIL 0x24`、`OK_V1 0x00 151` |
| 文書 | README に「エラーの見方」、TxStatus の例に 0x24。GAS「説明」に TxStatus と UploadErr の説明 | - |

- テスト 2 件追加(`test_summarize_failures`、`test_describe_error`)、既存テストに HTTP 500 と `last_ok` の確認を追加。計 10 件
- **`test_service_and_ctl` が 1 回だけ失敗した**(全体実行 5 回 + 単体 8 回のうち 1 回。`assert 7 ...`)。
  再現せず原因は未特定。10 秒周期の自動サイクルと送信の待ち時間(10 秒)の競合と推測しているが、
  今回の変更で起きるようになったのか、もとからあったのかは切り分けできていない
- GAS のテスト(`node gas/test_gas.js`)は Pi に node がなく未実行。`Code.gs` の変更は「説明」シートの文面 2 行だけ
- 導入確認のため `tanboctl poll` を 1 回実行したので、19:44:06 に 10 分境界でないサイクルが 1 つ記録されている
- 未確認の分類: `TIMEOUT` / `RESP` / `GAS` / DNS 失敗時の `NET` は実回線では出していない(`GAS` と `HTTP` はテストで確認)

## 2026-10-02 GAS への送信を開始

- ユーザーが tambo_data2 に `Code.gs`(コミット 519aea9 の版)をデプロイした。ブラウザで `/exec` を開いて `{"ok":true,...}` を確認済みとのこと
- ユーザーの了承のもと、URL とトークンを会話経由で受け取り、こちらで `/etc/tanbo/parent.toml` に書いた(値はここには書かない)。
  書き換え前の設定は `/etc/tanbo/parent.toml.bak`(仮の URL のもの、640 root:tanbo)
- 10:03:45 に `systemctl restart tanbo-parent`

### 結果

- 溜まっていた分がすべて送られた: 健全性 97 行、計測 1067 行(2026-10-01 18:10 〜 2026-10-02 10:00)。未送信 0 / 0
- 送信は 500 行ずつ。健全性 97 → 計測 500 → 500 までは約 17 秒で完了
- **3 回目(残り 67 行)で GAS が一度だけエラーを返した**:
  `GAS: This operation is not supported for this document: <スプレッドシート ID>`(10:04:18)。
  30 秒後の自動再送(10:04:53)で `uploaded 67 rows` / `upload recovered` となり解消。親機側では何もしていない
  - 原因は未特定。同じ呼び出しが 30 秒後には通ったので、Google 側の一時的なエラーと見ている。
    繰り返すようなら `health` シートの `UploadErr` に同じ文面が残るので、頻度を見る
  - エラー表示の改善(分類 `GAS:`、`送信エラー(時刻〜)`、`upload recovered`)は実回線でも意図どおりに出た
- シート側(`data_2026-10`、`health_2026-10`、`最新`、`親機`)はユーザーが見て「言われたとおりになっていると思う」との回答。
  行数や重複の有無を数えて確かめたかどうかまでは聞いていない

### 残っていること

- Pi 本体を再起動したあと自動で立ち上がるかの確認(未実施)
- モバイル通信のテスト(モデム未接続)、Phase 4(堅牢化)の提案
- GitHub への push(Pi に認証がない。ローカル `main` が origin より 5 コミット先)
- `test_service_and_ctl` の偶発的な失敗の原因
- 子機の v2 ファームへの入れ替え(現状は Node 2 が旧ファームで `OK_V1`)
