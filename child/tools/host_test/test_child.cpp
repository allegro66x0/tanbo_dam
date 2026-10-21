// 子機ファーム(tanbo_child_v2.ino)をパソコンで試験する。Linux / Pi で:
//   g++ -std=c++17 -I child/tools/host_test -x c++ child/tools/host_test/test_child.cpp -o /tmp/t && /tmp/t
#include "../../tanbo_child_v2/tanbo_child_v2.ino"

#include <cassert>
#include <iostream>

static int failures = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    if (!(cond)) {                                                      \
      std::cerr << "NG line " << __LINE__ << ": " #cond << std::endl;   \
      failures++;                                                       \
    }                                                                   \
  } while (0)

static std::vector<uint8_t> frame(const std::vector<uint8_t> &data) {
  std::vector<uint8_t> f = {0x7E, (uint8_t)(data.size() >> 8), (uint8_t)(data.size() & 0xFF)};
  uint8_t sum = 0;
  for (uint8_t b : data) sum += b;
  f.insert(f.end(), data.begin(), data.end());
  f.push_back((uint8_t)(0xFF - sum));
  return f;
}

static const uint8_t SRC64[8] = {0x00, 0x13, 0xA2, 0x00, 0x42, 0x3E, 0xAF, 0xE2};   // XBee 10 (par2)

static std::vector<uint8_t> rx_packet(const std::string &payload) {
  std::vector<uint8_t> d = {0x90};
  d.insert(d.end(), SRC64, SRC64 + 8);
  d.push_back(0x12);
  d.push_back(0x34);
  d.push_back(0x01);
  d.insert(d.end(), payload.begin(), payload.end());
  return frame(d);
}

struct Parsed {
  bool ok = false;
  uint8_t type = 0;
  std::vector<uint8_t> data;
};

// out から最初の API フレームを読む
static Parsed parse_frame(const std::vector<uint8_t> &out) {
  Parsed p;
  if (out.size() < 5 || out[0] != 0x7E) return p;
  size_t len = (out[1] << 8) | out[2];
  if (out.size() < 4 + len) return p;
  uint8_t sum = 0;
  for (size_t i = 0; i < len; i++) sum += out[3 + i];
  p.ok = (uint8_t)(sum + out[3 + len]) == 0xFF;
  p.type = out[3];
  p.data.assign(out.begin() + 3, out.begin() + 3 + len);
  return p;
}

static void run(int n = 5) {
  for (int i = 0; i < n; i++) {
    loop();
    g_millis += 10;
  }
}

static void reset_io() {
  Serial.in.clear(); Serial.out.clear();
  Serial2.in.clear(); Serial2.out.clear();
}

int main() {
  setup();

  // 1. 透過モード: 行の REQ には行の D2 を Serial2 に返す
  reset_io();
  Serial2.feed(std::string("REQ,5\n"));
  run();
  CHECK(Serial2.text().rfind("D2,5,7,7,2900,2900,2900,", 0) == 0);

  // 2. API モード: 0x90 の REQ には、送り主宛ての 0x10 で D2 を返す
  reset_io();
  Serial2.feed(rx_packet("REQ,77\n"));
  run();
  {
    Parsed p = parse_frame(Serial2.out);
    CHECK(p.ok);
    CHECK(p.type == 0x10);
    CHECK(p.data.size() > 14);
    CHECK(p.data[1] == 0x00);                                   // frame id 0(Transmit Status なし)
    CHECK(memcmp(p.data.data() + 2, SRC64, 8) == 0);            // 宛先 = 送り主
    CHECK(p.data[10] == 0x12 && p.data[11] == 0x34);            // 16bit も送り主
    std::string payload(p.data.begin() + 14, p.data.end());
    CHECK(payload.rfind("D2,77,7,7,2900,", 0) == 0);
    CHECK(payload.find("2.2.0\n") != std::string::npos);
    CHECK(Serial2.out.size() == 4 + p.data.size());             // 余計なバイトがない
  }

  // 3. チェックサムが壊れたフレームには返さない
  reset_io();
  {
    auto f = rx_packet("REQ,9\n");
    f.back() ^= 0x01;
    Serial2.feed(f);
  }
  run();
  CHECK(Serial2.out.empty());
  CHECK(Serial.text().find("checksum NG") != std::string::npos);

  // 4. Modem Status(0x8A)などは読み捨てる
  reset_io();
  Serial2.feed(frame({0x8A, 0x02}));
  run();
  CHECK(Serial2.out.empty());

  // 5. フレームが細切れに届いても読める
  reset_io();
  {
    auto f = rx_packet("REQ,123\n");
    for (uint8_t b : f) {
      Serial2.feed(std::vector<uint8_t>{b});
      loop();
      g_millis += 5;
    }
  }
  run();
  {
    Parsed p = parse_frame(Serial2.out);
    CHECK(p.ok && p.type == 0x10);
    std::string payload(p.data.begin() + 14, p.data.end());
    CHECK(payload.rfind("D2,123,", 0) == 0);
  }

  // 6. 途中で切れたフレームは時間切れで捨て、次のフレームは読める
  reset_io();
  {
    auto f = rx_packet("REQ,1\n");
    Serial2.feed(std::vector<uint8_t>(f.begin(), f.begin() + 8));
  }
  run(1);
  g_millis += 500;
  run(1);
  Serial2.feed(rx_packet("REQ,2\n"));
  run();
  {
    Parsed p = parse_frame(Serial2.out);
    CHECK(p.ok);
    std::string payload(p.data.begin() + 14, p.data.end());
    CHECK(payload.rfind("D2,2,", 0) == 0);
  }

  // 7. API で REQ 以外の文字列は返さない
  reset_io();
  Serial2.feed(rx_packet("HELLO\n"));
  run();
  CHECK(Serial2.out.empty());

  // 8. USB の REQ には USB に行で返す(XBee には何も出さない)
  reset_io();
  Serial.feed(std::string("REQ,42\n"));
  run();
  CHECK(Serial.text().find("\nD2,42,7,7,2900,") != std::string::npos ||
        Serial.text().rfind("D2,42,", 0) == 0 || Serial.text().find("D2,42,7,7") != std::string::npos);
  CHECK(Serial2.out.empty());

  // 9. エコーなし(タイムアウト)なら n_ok = 0 と -1
  reset_io();
  g_echo_us = 0;
  Serial2.feed(rx_packet("REQ,8\n"));
  run();
  {
    Parsed p = parse_frame(Serial2.out);
    std::string payload(p.data.begin() + 14, p.data.end());
    CHECK(payload.rfind("D2,8,0,7,-1,-1,-1,", 0) == 0);
  }
  g_echo_us = 2900;

  if (failures) {
    std::cerr << failures << " failure(s)" << std::endl;
    return 1;
  }
  std::cout << "all host tests passed" << std::endl;
  return 0;
}
