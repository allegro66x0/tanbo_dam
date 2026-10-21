// 子機ファームをパソコンで試験するための、Arduino の最小限の代わり(test_child.cpp から使う)
#pragma once
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <string>
#include <vector>

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define SERIAL_8N1 0

inline unsigned long g_millis = 0;
inline long g_echo_us = 2900;   // pulseIn が返すエコー時間

inline unsigned long millis() { return g_millis; }
inline void delay(unsigned long ms) { g_millis += ms; }
inline void delayMicroseconds(unsigned int) {}
inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}
inline unsigned long pulseIn(int, int, unsigned long) { return (unsigned long)g_echo_us; }

class Stream {
 public:
  std::deque<uint8_t> in;
  std::vector<uint8_t> out;
  void begin(unsigned long, int = 0, int = -1, int = -1) {}
  int available() { return (int)in.size(); }
  int read() {
    if (in.empty()) return -1;
    int c = in.front();
    in.pop_front();
    return c;
  }
  size_t write(uint8_t b) { out.push_back(b); return 1; }
  size_t write(const uint8_t *p, size_t n) { out.insert(out.end(), p, p + n); return n; }
  size_t print(const char *s) { return write((const uint8_t *)s, strlen(s)); }
  size_t println(const char *s) { size_t n = print(s); return n + print("\n"); }
  size_t printf(const char *fmt, ...) {
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    return print(buf), (size_t)n;
  }
  void feed(const std::vector<uint8_t> &b) { in.insert(in.end(), b.begin(), b.end()); }
  void feed(const std::string &s) { in.insert(in.end(), s.begin(), s.end()); }
  std::string text() const { return std::string(out.begin(), out.end()); }
};

inline Stream Serial;
inline Stream Serial2;

struct EspClass {
  int restarts = 0;
  void restart() { restarts++; }
};
inline EspClass ESP;
