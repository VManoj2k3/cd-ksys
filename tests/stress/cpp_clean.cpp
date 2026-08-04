// FP-bait: correct modern C++14 with RAII. Layers + LLM verifier must stay silent.
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace net {

class Connection {
public:
    explicit Connection(std::string host) : host_(std::move(host)) {}
    ~Connection() = default;
    Connection(const Connection &) = delete;
    Connection &operator=(const Connection &) = delete;
    Connection(Connection &&) noexcept = default;
    Connection &operator=(Connection &&) noexcept = default;

    const std::string &host() const { return host_; }

private:
    std::string host_;
};

class Pool {
public:
    void add(std::string host) {
        std::lock_guard<std::mutex> lock(mu_);
        conns_.push_back(std::make_unique<Connection>(std::move(host)));
    }

    std::size_t size() const {
        std::lock_guard<std::mutex> lock(mu_);
        return conns_.size();
    }

private:
    mutable std::mutex mu_;
    std::vector<std::unique_ptr<Connection>> conns_;
};

}  // namespace net
