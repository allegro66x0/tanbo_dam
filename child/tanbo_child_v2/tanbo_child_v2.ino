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
// - XBee は透過モード(AP=0)、DH/DL=0(宛先コーディネータ)、BD=9600
//
// 配線(旧子機と同じ):
//   HC-SR04 TRIG ← GPIO4、ECHO → 10k/20k 分圧 → GPIO18
//   XBee DOUT → GPIO25(RX)、XBee DIN ← GPIO26(TX)  ※WROVER は GPIO16/17 使用不可(PSRAM)
// =============================================================================

#include <Arduino.h>

#define FW_VERSION "2.0.0"

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
const unsigned long LINE_IDLE_MS = 50;  // 改行なしでもこの時間無通信なら1行とみなす(旧親機互換)
const size_t LINE_MAX = 64;
// 親機からの REQ がこの時間途絶えたら自分を再起動(UART 固着などへの保険)
const unsigned long NO_REQ_RESTART_MS = 6UL * 3600UL * 1000UL;

char line_buf[LINE_MAX + 1];
size_t line_len = 0;
unsigned long last_rx_ms = 0;
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

void measure_and_reply(long seq) {
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

  char out[96];
  snprintf(out, sizeof(out), "D2,%ld,%d,%d,%ld,%ld,%ld,%lu,%s\n",
           seq, n_ok, N_TRY, med, mn, mx, millis() / 1000UL, FW_VERSION);
  Serial2.print(out);
  Serial.print("[REQ] -> ");
  Serial.print(out);
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

void handle_line() {
  line_buf[line_len] = '\0';
  // 前後の空白・CR を除去
  size_t start = 0;
  while (start < line_len && (line_buf[start] == ' ' || line_buf[start] == '\r')) start++;
  size_t end = line_len;
  while (end > start && (line_buf[end - 1] == ' ' || line_buf[end - 1] == '\r')) end--;
  line_buf[end] = '\0';
  const char *s = line_buf + start;
  line_len = 0;
  if (*s == '\0') return;

  long seq = parse_req(s);
  if (seq == -2) {
    Serial.print("[?] ");
    Serial.println(s);
    return;
  }
  last_req_ms = millis();
  measure_and_reply(seq);
}

void poll_serial() {
  while (Serial2.available() > 0) {
    int c = Serial2.read();
    if (c < 0) break;
    last_rx_ms = millis();
    if (c == '\n') {
      handle_line();
    } else if (line_len < LINE_MAX) {
      line_buf[line_len++] = (char)c;
    } else {
      line_len = 0;   // 長すぎる行は捨てる
    }
  }
  if (line_len > 0 && millis() - last_rx_ms >= LINE_IDLE_MS) {
    handle_line();
  }
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
  poll_serial();
  if (millis() - last_req_ms > NO_REQ_RESTART_MS) {
    Serial.println("no REQ for a long time -> restart");
    delay(100);
    ESP.restart();
  }
  delay(1);
}
