# 田んぼダム 水位計測システム v2 — 作業メモ

用水路の水位を多点で長期計測する卒業研究のシステム。詳細な導入手順は README.md、通信仕様は docs/PROTOCOL.md。

## Pi 上で作業する場合

`docs/HANDOFF_PI.md` の手順に従い、結果を `docs/pi_notes.md` に追記する。

## 構成

- `parent/` 親機(Raspberry Pi 4、Python 3.11+)。パッケージ `tanbo`、systemd サービス `tanbo-parent`、操作 CLI `tanboctl`
  - poller.py: 壁時計に揃えた周期で全ノードへ `REQ,<seq>` → seq 一致の応答だけ採用
  - store.py: SQLite(WAL)に先に書く store-and-forward
  - uploader.py: 未送信をまとめて GAS へ POST、返ってきた hwm まで送信済みにする
  - radio.py: digi-xbee、送信は非同期、Transmit Status と受信をイベントキューへ
- `gas/` 受信 GAS。書き込み先はスプレッドシート **tambo_data2**(`SPREADSHEET_ID`)。旧 `tanbo_data` は凍結
- `child/tanbo_child_v2/` 子機ファーム(ESP32-WROVER + HC-SR04)。全子機同一コード
- `docs/PROTOCOL.md` 子機↔親機プロトコル v2

## 決定事項

- 子機 XBee は透過モード AP=0(DH/DL=0)、親機 XBee は API モード AP=1。BD=9600
- ノード番号は親機が子機 XBee の 64bit アドレスから決める(`[nodes]` 設定)。子機は番号を持たない
- 子機は距離ではなくエコー時間(往復 µs)の中央値・最小・最大を返す。温度補正は後処理
- 周期: 通常 10 分。設置チェック時は `tanboctl check` で一時的に 60 秒(期限付きで自動復帰)
- deep sleep は見送り(子機は常時起動、親機からのポーリング)

## テスト

```bash
cd parent && python -m pytest -q tests   # 実 digi-xbee + pty の XBee エミュレータ + 偽 GAS(Linux / WSL)
node gas/test_gas.js                     # Code.gs を Apps Script モック上で
```

pty を使うので親機テストは Windows ネイティブでは動かない。WSL か Pi 上で実行する。
