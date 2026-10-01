# Pi 上の Claude Code への引き継ぎ(親機 v2 の実機導入)

このリポジトリの v2 親機を、Raspberry Pi 4(本番の親機)で動かせる状態にするのが目的。
コードはクラウド側でテスト済みだが、**実機(XBee・モデム・systemd)では一度も動かしていない**。
実機でしか分からないことを確かめ、必要なら直し、サービスとして常駐させるまでを担当してほしい。

先に `CLAUDE.md`、`README.md`、`docs/PROTOCOL.md` を読むこと。

## 守ること

- 次の操作は**実行前にユーザーに確認する**: 旧親機プログラムの停止・無効化、`/etc` 配下の変更、
  パーティション・overlayfs・fstab の変更、再起動、パッケージの削除
- `/etc/tanbo/parent.toml` の `token` と `url` はユーザーが自分で書く。値をログや会話に出さない、コミットしない
- プロトコル(子機↔親機)や GAS に送る列を変えるときは、`docs/PROTOCOL.md`・`gas/Code.gs`・テストを同時に直す
- テスト(`parent/tests`)は常に通る状態を保つ。直したら `python -m pytest -q parent/tests`
- 調べたこと・やったこと・判断に迷ったことは `docs/pi_notes.md` に日付付きで追記する(チャット側の Claude がここを読んで続きを考える)
- コミットは小さく、メッセージに何を確かめたかを書く

## Phase 0: 現状調査(何も変更しない)

次を調べて `docs/pi_notes.md` にまとめ、ユーザーに報告してから Phase 1 へ。

- OS と Python のバージョン(v2 は Python 3.11 以上が必要。`tomllib` を使う)
- 旧親機プログラムの所在と起動方法: `systemctl list-units --type=service`、`crontab -l`、`/etc/rc.local`、
  `ps aux | grep -i -E "python|xbee"`。旧コードは `/home/pi0/xbee_sensor` 付近の可能性あり
- 親機 XBee のシリアル: `ls -l /dev/serial/by-id/`、どのプロセスが掴んでいるか(`fuser` / `lsof`)
- モバイル通信モジュール: `lsusb`、`ip a`、`mmcli -L`(ModemManager 管理下か)。`/dev/ttyUSB*` を何本生やしているか
- 時刻同期: `timedatectl`(timesyncd か chrony か)
- I2C: `/dev/i2c-1` の有無、`i2cdetect -y 1`(i2c-tools があれば)
- ストレージ: `lsblk`、`df -h`、overlayfs の状態(`raspi-config nonint get_overlay_now` など)
- ユーザーとグループ: `dialout`・`i2c` グループの存在

## Phase 1: テスト

```bash
cd parent
python3 -m venv .venv && . .venv/bin/activate
pip install -e . pytest
python -m pytest -q tests
```

Pi は遅いので、時間依存のアサーションで落ちたら環境由来か本当の不具合かを切り分けて報告する
(テスト側の閾値を緩めるだけで済ませる場合は理由を pi_notes に残す)。

## Phase 2: 実機 XBee での疎通(旧プログラム停止の許可をもらってから)

1. 旧プログラムを止める(無効化はまだしない。戻せるように)
2. 一時設定を作る(リポジトリ外、例 `~/tanbo_test.toml`)。`port` は by-id、`db_path` は `~/tanbo_test.db`、
   `control.socket` は `~/tanbo_test.sock`、`interval_s = 60`。`upload.url` は未設定のダミーでよい
   (送信は失敗してバックオフするが、計測は SQLite に溜まる)
3. フォアグラウンドで起動: `python -m tanbo -c ~/tanbo_test.toml -v`
   - `XBee open: ... MAC=...` が出ること(出なければ AP=1 か、ボーレート、ポートを確認)
4. 別端末で `python -m tanbo.ctl --socket ~/tanbo_test.sock status` と `get <node>`
   - 現状の子機は旧ファームなので `OK_V1` / `NO_DATA` が正常。届かないノードは `TX_FAIL`
5. 各ノードの status と所要時間(`cycle_ms`)を pi_notes に記録

ここで分かった実機との差(digi-xbee の挙動、Transmit Status のコード、タイミングなど)は修正し、
テストのエミュレータ(`parent/tests/xbee_emu.py`)にも反映する。

## Phase 3: サービス化

README の「2. 親機 / インストール」に沿って `/opt/tanbo` へ入れ、systemd で常駐させる。

- `parent.toml` はユーザーに `url` と `token` を書いてもらう(GAS は README の「1. GAS」で tambo_data2 にデプロイ済みのはず。未完了ならユーザーに伝えて待つ)
- 起動後に確認すること:
  - `systemctl status tanbo-parent` が active、`journalctl -u tanbo-parent` にエラーがない
  - `tanboctl status` で次回時刻が 10 分境界、未送信件数が減っていく
  - tambo_data2 に `data_YYYY-MM` / `health_YYYY-MM` が増える(ユーザーに見てもらう)
  - `sudo systemctl kill -s SIGSTOP tanbo-parent` で止めると WatchdogSec(300 秒)後に再起動される
- 旧プログラムの無効化はこの段階で、ユーザーの確認を取ってから

## Phase 4: 堅牢化(提案だけ。実施はユーザーと相談)

- ハードウェアウォッチドッグ(`/etc/systemd/system.conf` の `RuntimeWatchdogSec`)
- SD 保護: overlayfs + `/var/lib/tanbo` を書き込み可能な別パーティションへ。現状のパーティション構成で
  どう実現するか、手順と戻し方を pi_notes に書いて提案する
- モデムの自己回復(疎通がなければ再接続、だめなら USB 給電の入れ直し)の要否と方法
