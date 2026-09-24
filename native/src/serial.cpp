#include "serial.h"

#if defined(_WIN32)
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#elif defined(__linux__)
#  include <asm/ioctls.h>       // TCGETS2/TCSETS2: musl's sys/ioctl.h leaves them out
#  include <asm/termbits.h>     // termios2 + BOTHER: 256000 baud is not a Bxxxx constant
#  include <fcntl.h>
#  include <poll.h>
#  include <sys/ioctl.h>
#  include <unistd.h>
#endif

namespace wavr {

namespace {

#if defined(_WIN32)
class WinSerial : public SerialPort {
 public:
  explicit WinSerial(HANDLE h) : h_(h) {}
  ~WinSerial() override { CloseHandle(h_); }
  long read(uint8_t* buf, size_t len, int timeout_ms) override {
    COMMTIMEOUTS t{};
    t.ReadIntervalTimeout = 20;
    t.ReadTotalTimeoutConstant = static_cast<DWORD>(timeout_ms);
    SetCommTimeouts(h_, &t);
    DWORD got = 0;
    if (!ReadFile(h_, buf, static_cast<DWORD>(len), &got, nullptr)) return -1;
    return static_cast<long>(got);
  }

 private:
  HANDLE h_;
};
#elif defined(__linux__)
class LinuxSerial : public SerialPort {
 public:
  explicit LinuxSerial(int fd) : fd_(fd) {}
  ~LinuxSerial() override { ::close(fd_); }
  long read(uint8_t* buf, size_t len, int timeout_ms) override {
    pollfd p{fd_, POLLIN, 0};
    int rc = poll(&p, 1, timeout_ms);
    if (rc < 0) return -1;
    if (rc == 0) return 0;
    if (p.revents & (POLLERR | POLLHUP)) return -1;
    ssize_t n = ::read(fd_, buf, len);
    return n < 0 ? -1 : static_cast<long>(n);
  }

 private:
  int fd_;
};
#endif

}  // namespace

std::unique_ptr<SerialPort> open_serial(const std::string& name, int baud, std::string* error) {
#if defined(_WIN32)
  std::string path = name.rfind("\\\\.\\", 0) == 0 ? name : "\\\\.\\" + name;
  HANDLE h = CreateFileA(path.c_str(), GENERIC_READ, 0, nullptr, OPEN_EXISTING, 0, nullptr);
  if (h == INVALID_HANDLE_VALUE) {
    *error = "cannot open " + name;
    return nullptr;
  }
  DCB dcb{};
  dcb.DCBlength = sizeof dcb;
  GetCommState(h, &dcb);
  dcb.BaudRate = static_cast<DWORD>(baud);
  dcb.ByteSize = 8;
  dcb.Parity = NOPARITY;
  dcb.StopBits = ONESTOPBIT;
  dcb.fBinary = TRUE;
  if (!SetCommState(h, &dcb)) {
    CloseHandle(h);
    *error = "cannot set " + std::to_string(baud) + " baud on " + name;
    return nullptr;
  }
  return std::make_unique<WinSerial>(h);
#elif defined(__linux__)
  int fd = ::open(name.c_str(), O_RDONLY | O_NOCTTY | O_CLOEXEC);
  if (fd < 0) {
    *error = "cannot open " + name;
    return nullptr;
  }
  termios2 t{};
  if (ioctl(fd, TCGETS2, &t) != 0) {
    ::close(fd);
    *error = name + " is not a serial port";
    return nullptr;
  }
  t.c_cflag &= ~(CBAUD | CSIZE | PARENB | CSTOPB | CRTSCTS);
  t.c_cflag |= BOTHER | CS8 | CLOCAL | CREAD;
  t.c_iflag = 0;
  t.c_oflag = 0;
  t.c_lflag = 0;
  t.c_ispeed = t.c_ospeed = static_cast<speed_t>(baud);
  if (ioctl(fd, TCSETS2, &t) != 0) {
    ::close(fd);
    *error = "cannot set " + std::to_string(baud) + " baud on " + name;
    return nullptr;
  }
  return std::make_unique<LinuxSerial>(fd);
#else
  (void)baud;
  *error = "serial ports are not supported on this platform yet (" + name + ")";
  return nullptr;
#endif
}

}  // namespace wavr
