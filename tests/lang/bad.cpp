// Planted violations for the C++ pipeline (AUTOSAR-relevant patterns).
#include <cstdio>
#include <cstdlib>
#include <cstring>

class PacketRecieved {                 // misspelled identifier -> spell
public:
    explicit PacketRecieved(int size) : size_(size) {
        data_ = new char[static_cast<unsigned>(size)];
    }
    // no destructor / copy control -> leak + rule-of-five (LLM/tidy)
    int size_;
    char *data_;
};

int parse_value(const char *text) {
    int value = (int)strtol(text, nullptr, 10);   // C-style cast -> clang-tidy
    return value;
}

const int &dangling_ref() {
    int local = 5;
    return local;                       // returning ref to local -> tidy/cppcheck
}

int main() {
    PacketRecieved pkt(64);
    std::printf("%d %d\n", pkt.size_, parse_value("12"));
    return 0;
}
