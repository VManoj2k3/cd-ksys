// FN-bait C++: subtle bugs. Some caught by cppcheck/clang-tidy, the rest by LLM.
#include <cstring>
#include <vector>

// [LLM] use-after-move: v is used after being moved from
std::vector<int> use_after_move(std::vector<int> v) {
    std::vector<int> other = std::move(v);
    v.push_back(1);          // BUG: v is in moved-from state
    return other;
}

// [cppcheck/LLM] raw new without matching delete on all paths
class Widget {
public:
    Widget() : data_(new int[64]) {}
    // BUG: no destructor -> leaks data_ (and no rule-of-5)
    int *data_;
};

// [LLM] iterator invalidation: erasing while iterating with ++it
void remove_zeros(std::vector<int> &v) {
    for (auto it = v.begin(); it != v.end(); ++it) {
        if (*it == 0) {
            v.erase(it);     // BUG: it invalidated after erase
        }
    }
}

// [LLM] returning reference to local temporary
const std::string &bad_ref() {
    return std::string("temp");   // BUG: dangling reference
}

// [LLM] dereference potentially-null after bad cast
int deref(void *p) {
    int *ip = static_cast<int *>(p);
    return *ip;              // BUG: no null check; p may be null
}
