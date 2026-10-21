// =============================================================================
// tanbo_child_v2.ino — 田んぼダム 子機(ESP32-WROVER + HC-SR04 + XBee 3)
//
// プロトコル v2(docs/PROTOCOL.md):
//   親機 → 子機:  REQ,<seq>\n
//   子機 → 親機:  D2,<seq>,<n_ok>,<n_try>,<med_us>,<min_us>,<max_us>,<uptime_s>,<fw>\n
//
// - REQ を受けたときだけ、その場でバースト測定(N_TRY 回)して中央値を返す。自発送信はしない
// - 距離ではなくエコー時間(往復 µs)を返す。音速の温度補正は後処理で行う
// - ノード番号は持たない(親機が XBee の MAC で判定)。全子機で同一コード
// - XBee は BD=9600。AP=0(透過)と AP=1(API、エスケープなし)のどちらでも動く。受信の先頭が 0x7E なら API のフレーム
//   - AP=1: REQ を送ってきた親機(受信フレームの送り主)へ D2 を返す。親機が 2 台あってもそれぞれに返る
//   - AP=0: XBee の DH/DL の宛先へ返す(DH/DL=0 ならコーディネータ)。v2.1 までと同じ
// - USB シリアル(115200)からの REQ にも同じ形式で USB に返す。親機に USB でつないで
//   「親機の地点のセンサ」として使うため(XBee なしで可)。USB にはデバッグ出力も流れるが、
//   親機は D2 で始まる行だけを読む
//
// 配線(旧子機と同じ):
//   HC-SR04 TRIG ← GPIO4、ECHO → 10k/20k 分圧 → GPIO18
//   XBee DOUT → GPIO25(RX)、XBee DIN ← GPIO26(TX)  ※WROVER は GPIO16/17 使用不可(PSRAM)
// =============================================================================

#include <Arduino.h>

#define FW_VERSION "2.2.0"

#define TRIG_PIN 4
#define ECHO_PIN 18
#define RXD2 25
#define TXD2 26

// ---- 測定パラメータ ----------------------------------------------------------
const int N_TRY = 7;                        // 1回の REQ で測る回数(奇数)
const unsigned long PING_GAP_MS = 60;       // HC-SR04 の推奨最小間隔(残響を避ける)
const unsigned long ECHO_TIMEOUT_US = 25000;  // pulseIn の待ち上限(約 4.3m 相当)
// 有効とみなすエコー時間の範囲(往復 µs、20℃換算: cm ≒ us / 58.2)
const long MIN_VALID_US = 290;    // 約 5cm 未満は近距離の残響として捨てる(ブランキング)
const long MAX_VALID_US = 11650;  // 約 2m 超は管外・多重反射として捨てる

// ---- 受信 ---------------------------------------------------------------------
const unsigned long LINE_IDLE_MS = 50;
const size_t RX_LINE_MAX = 64;
const size_t API_FRAME_MAX = 128;           // 受け付ける API フレームのデータ長の上限
const unsigned long API_FRAME_IDLE_MS = 200;  // フレームの途中でこの時間止まったら捨てる
// 親機からの REQ がこの時間途絶えたら自分を再起動(UART 固着などへの保険)
const unsigned long NO_REQ_RESTART_MS = 6UL * 3600UL * 1000UL;

// 受信口ごとの行バッファ(XBee と USB)
struct LineReader {
  char buf[RX_LINE_MAX + 1];
  size_t len;
  unsigned long last_rx_ms;
};
LineReader xbee_rx = {};
LineReader usb_rx = {};

// XBee の API フレーム(0x7E, 長さ 2 バイト, データ, チェックサム)の受信状態
struct ApiReader {
  bool active;
  size_t pos;          // 0x7E の後に読んだバイト数
  size_t len;          // データ長(長さフィールド)
  uint8_t buf[API_FRAME_MAX];
  uint8_t sum;
  unsigned long last_rx_ms;
};
ApiReader api_rx = {};

unsigned long last_req_ms = 0;

// -----------------------------------------------------------------------------

long ping_once_us() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  return (long)pulseIn(ECHO_PIN, HIGH, ECHO_TIMEOUT_US);   // 0 = タイムアウト
}

void sort_long(long *a, int n) {
  for (int i = 1; i < n; i++) {
    long v = a[i];
    int j = i - 1;
    while (j >= 0 && a[j] > v) {
      a[j + 1] = a[j];
      j--;
    }
    a[j + 1] = v;
  }
}

// バースト測定して D2 行を out に書く
void measure(long seq, char *out, size_t size) {
  long ok_us[N_TRY];
  int n_ok = 0;
  for (int i = 0; i < N_TRY; i++) {
    if (i > 0) delay(PING_GAP_MS);
    long us = ping_once_us();
    if (us >= MIN_VALID_US && us <= MAX_VALID_US) ok_us[n_ok++] = us;
    Serial.printf("  ping %d: %ld us%s\n", i, us,
                  (us >= MIN_VALID_US && us <= MAX_VALID_US) ? "" : " (rejected)");
  }

  long med = -1, mn = -1, mx = -1;
  if (n_ok > 0) {
    sort_long(ok_us, n_ok);
    med = (n_ok % 2) ? ok_us[n_ok / 2] : (ok_us[n_ok / 2 - 1] + ok_us[n_ok / 2]) / 2;
    mn = ok_us[0];
    mx = ok_us[n_ok - 1];
  }
  snprintf(out, size, "D2,%ld,%d,%d,%ld,%ld,%ld,%lu,%s\n",
           seq, n_ok, N_TRY, med, mn, mx, millis() / 1000UL, FW_VERSION);
}

// "REQ" または "REQ,<seq>" を含む行なら seq(なければ -1)を返す。REQ でなければ -2
long parse_req(const char *s) {
  const char *p = strstr(s, "REQ");
  if (p == NULL) return -2;
  p += 3;
  if (*p != ',') return -1;
  p++;
  if (*p < '0' || *p > '9') return -1;
  long v = 0;
  while (*p >= '0' && *p <= '9' && v <= 65535) v = v * 10 + (*p++ - '0');
  return (v <= 65535) ? v : -1;
}

// 前後の空白・CR・LF を除いた先頭を返す(s は書き換える)
char *strip(char *s, size_t len) {
  size_t start = 0;
  while (start < len && (s[start] == ' ' || s[start] == '\r' || s[start] == '\n')) start++;
  size_t end = len;
  while (end > start && (s[end - 1] == ' ' || s[end - 1] == '\r' || s[end - 1] == '\n')) end--;
  s[end] = '\0';
  return s + start;
}

// ---- 透過モード(AP=0)と USB: 行単位 ----------------------------------------

void handle_line(LineReader &lr, Stream &reply_to) {
  lr.buf[lr.len] = '\0';
  const char *s = strip(lr.buf, lr.len);
  lr.len = 0;
  if (*s == '\0') return;

  long seq = parse_req(s);
  if (seq == -2) {
    Serial.print("[?] ");
    Serial.println(s);
    return;
  }
  last_req_ms = millis();
  char out[96];
  measure(seq, out, sizeof(out));
  reply_to.print(out);   // REQ が届いた口(XBee なら Serial2、USB なら Serial)に返す
  if (&reply_to != &Serial) {
    Serial.print("[REQ] -> ");
    Serial.print(out);
  }
}

void line_byte(LineReader &lr, Stream &port, int c) {
  lr.last_rx_ms = millis();
  if (c == '\n') {
    handle_line(lr, port);
  } else if (lr.len < RX_LINE_MAX) {
    lr.buf[lr.len++] = (char)c;
  } else {
    lr.len = 0;   // 長すぎる行は捨てる
  }
}

void line_idle(LineReader &lr, Stream &port) {
  if (lr.len > 0 && millis() - lr.last_rx_ms >= LINE_IDLE_MS) {
    handle_line(lr, port);
  }
}

// ---- API モード(AP=1): フレーム単位 -----------------------------------------

// 0x10 Transmit Request で dest64 へ送る。frame_id = 0 なので Transmit Status は返らない
void api_send(const uint8_t *dest64, const uint8_t *dest16, const char *payload) {
  size_t n = strlen(payload);
  uint8_t head[14] = {0x10, 0x00};
  memcpy(head + 2, dest64, 8);
  memcpy(head + 10, dest16, 2);
  head[12] = 0x00;   // broadcast radius
  head[13] = 0x00;   // options
  size_t len = sizeof(head) + n;
  uint8_t sum = 0;
  for (size_t i = 0; i < sizeof(head); i++) sum += head[i];
  for (size_t i = 0; i < n; i++) sum += (uint8_t)payload[i];
  Serial2.write(0x7E);
  Serial2.write((uint8_t)(len >> 8));
  Serial2.write((uint8_t)(len & 0xFF));
  Serial2.write(head, sizeof(head));
  Serial2.write((const uint8_t *)payload, n);
  Serial2.write((uint8_t)(0xFF - sum));
}

void handle_api_frame(const uint8_t *d, size_t len) {
  if (len < 1) return;
  if (d[0] != 0x90) {   // 0x90 Receive Packet 以外(Modem Status など)は読み捨てる
    Serial.printf("[api] frame 0x%02X (%u bytes) ignored\n", d[0], (unsigned)len);
    return;
  }
  if (len < 12) return;
  const uint8_t *src64 = d + 1;
  const uint8_t *src16 = d + 9;
  char text[RX_LINE_MAX + 1];
  size_t n = len - 12;
  if (n > RX_LINE_MAX) n = RX_LINE_MAX;
  memcpy(text, d + 12, n);
  text[n] = '\0';
  const char *s = strip(text, n);

  char from[17];
  for (int i = 0; i < 8; i++) snprintf(from + i * 2, 3, "%02X", src64[i]);
  long seq = parse_req(s);
  if (seq == -2) {
    Serial.printf("[?] from %s: %s\n", from, s);
    return;
  }
  last_req_ms = millis();
  char out[96];
  measure(seq, out, sizeof(out));
  api_send(src64, src16, out);   // 送ってきた親機へ返す
  Serial.printf("[REQ from %s] -> %s", from, out);
}

// 戻り値: このバイトを API フレームとして消費したか
bool api_byte(ApiReader &ar, int c) {
  if (!ar.active) {
    if (c != 0x7E) return false;
    ar.active = true;
    ar.pos = 0;
    ar.len = 0;
    ar.sum = 0;
    ar.last_rx_ms = millis();
    return true;
  }
  ar.last_rx_ms = millis();
  uint8_t b = (uint8_t)c;
  if (ar.pos == 0) {
    ar.len = (size_t)b << 8;
  } else if (ar.pos == 1) {
    ar.len |= b;
    if (ar.len == 0 || ar.len > API_FRAME_MAX) ar.active = false;   // 長すぎる・壊れたフレームは捨てる
  } else if (ar.pos < 2 + ar.len) {
    ar.buf[ar.pos - 2] = b;
    ar.sum += b;
  } else {
    if ((uint8_t)(ar.sum + b) == 0xFF) {
      handle_api_frame(ar.buf, ar.len);
    } else {
      Serial.println("[api] checksum NG");
    }
    ar.active = false;
    return true;
  }
  ar.pos++;
  return true;
}

void poll_xbee() {
  while (Serial2.available() > 0) {
    int c = Serial2.read();
    if (c < 0) break;
    // 行の途中でなければ、0x7E は API フレームの始まり(透過モードの REQ には 0x7E = '~' は出てこない)
    if (xbee_rx.len == 0 && api_byte(api_rx, c)) continue;
    line_byte(xbee_rx, Serial2, c);
  }
  if (api_rx.active && millis() - api_rx.last_rx_ms >= API_FRAME_IDLE_MS) {
    api_rx.active = false;   // 途中で切れたフレームは捨てる
  }
  line_idle(xbee_rx, Serial2);
}

void poll_usb() {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    line_byte(usb_rx, Serial, c);
  }
  line_idle(usb_rx, Serial);
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(9600, SERIAL_8N1, RXD2, TXD2);
  pinMode(TRIG_PIN, OUTPUT);
  digitalWrite(TRIG_PIN, LOW);
  pinMode(ECHO_PIN, INPUT);
  last_req_ms = millis();
  Serial.println("=== tanbo child v" FW_VERSION " ===");
}

void loop() {
  poll_xbee();
  poll_usb();
  if (millis() - last_req_ms > NO_REQ_RESTART_MS) {
    Serial.println("no REQ for a long time -> restart");
    delay(100);
    ESP.restart();
  }
  delay(1);
}
