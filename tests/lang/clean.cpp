// Clean C++14 — deterministic layers must stay silent.
#include <array>
#include <cstdio>
#include <memory>
#include <string>

namespace sensors {

constexpr std::size_t kMaxReadings = 4U;

class Reading {
public:
    Reading(unsigned int id, long value) : id_(id), value_(value) {}
    long value() const { return value_; }
    unsigned int id() const { return id_; }

private:
    unsigned int id_;
    long value_;
};

inline long total(const std::array<Reading, kMaxReadings> &readings) {
    long sum = 0L;
    for (const auto &reading : readings) {
        sum += reading.value();
    }
    return sum;
}

}  // namespace sensors

int main() {
    const std::array<sensors::Reading, sensors::kMaxReadings> readings{{
        {1U, 10L}, {2U, 20L}, {3U, 30L}, {4U, 40L},
    }};
    std::printf("total=%ld\n", sensors::total(readings));
    return 0;
}
