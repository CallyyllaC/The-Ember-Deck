#include "protocol.hpp"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <limits>

namespace bridge::json {
namespace {

class Parser {
public:
    explicit Parser(std::string_view text) : text_(text) {}

    std::optional<Object> parse() {
        skip_ws();
        auto object = parse_object(0);
        if (!object) return std::nullopt;
        skip_ws();
        if (pos_ != text_.size()) return std::nullopt;  // trailing garbage
        return object;
    }

private:
    std::string_view text_;
    std::size_t pos_ = 0;

    bool eof() const { return pos_ >= text_.size(); }
    char peek() const { return eof() ? '\0' : text_[pos_]; }

    void skip_ws() {
        while (!eof() && (text_[pos_] == ' ' || text_[pos_] == '\t' || text_[pos_] == '\r' || text_[pos_] == '\n')) ++pos_;
    }

    bool consume(char c) {
        if (peek() != c) return false;
        ++pos_;
        return true;
    }

    bool literal(std::string_view word) {
        if (text_.substr(pos_, word.size()) != word) return false;
        pos_ += word.size();
        return true;
    }

    std::optional<Object> parse_object(int depth) {
        if (depth > 8 || !consume('{')) return std::nullopt;
        Object result;
        skip_ws();
        if (consume('}')) return result;
        while (true) {
            skip_ws();
            auto key = parse_string();
            if (!key) return std::nullopt;
            skip_ws();
            if (!consume(':')) return std::nullopt;
            skip_ws();
            auto value = parse_value(depth);
            if (!value) return std::nullopt;
            if (result.count(*key)) return std::nullopt;  // duplicate keys are rejected
            result.emplace(std::move(*key), std::move(*value));
            skip_ws();
            if (consume('}')) return result;
            if (!consume(',')) return std::nullopt;
        }
    }

    std::optional<Value> parse_value(int depth) {
        char c = peek();
        if (c == '"') {
            auto s = parse_string();
            if (!s) return std::nullopt;
            return Value(std::move(*s));
        }
        if (c == '{') {
            auto o = parse_object(depth + 1);
            if (!o) return std::nullopt;
            return Value(std::move(*o));
        }
        if (literal("true")) return Value(true);
        if (literal("false")) return Value(false);
        if (literal("null")) return Value(nullptr);
        if (c == '-' || std::isdigit(static_cast<unsigned char>(c))) return parse_int();
        return std::nullopt;  // arrays and anything else are unsupported
    }

    std::optional<Value> parse_int() {
        bool negative = consume('-');
        if (eof() || !std::isdigit(static_cast<unsigned char>(peek()))) return std::nullopt;
        if (peek() == '0' && pos_ + 1 < text_.size() && std::isdigit(static_cast<unsigned char>(text_[pos_ + 1])))
            return std::nullopt;  // no leading zeros
        std::uint64_t magnitude = 0;
        while (!eof() && std::isdigit(static_cast<unsigned char>(peek()))) {
            unsigned digit = static_cast<unsigned>(text_[pos_++] - '0');
            if (magnitude > (std::numeric_limits<std::uint64_t>::max() - digit) / 10) return std::nullopt;
            magnitude = magnitude * 10 + digit;
        }
        if (peek() == '.' || peek() == 'e' || peek() == 'E') return std::nullopt;  // no floats
        const auto max = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
        if (!negative && magnitude > max) return std::nullopt;
        if (negative && magnitude > max + 1) return std::nullopt;
        std::int64_t value = negative ? static_cast<std::int64_t>(0 - magnitude) : static_cast<std::int64_t>(magnitude);
        return Value(value);
    }

    static void append_utf8(std::string& out, std::uint32_t cp) {
        if (cp < 0x80) {
            out += static_cast<char>(cp);
        } else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (cp >> 18));
            out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        }
    }

    std::optional<std::uint32_t> hex4() {
        if (pos_ + 4 > text_.size()) return std::nullopt;
        std::uint32_t v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = text_[pos_++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= static_cast<std::uint32_t>(c - '0');
            else if (c >= 'a' && c <= 'f') v |= static_cast<std::uint32_t>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= static_cast<std::uint32_t>(c - 'A' + 10);
            else return std::nullopt;
        }
        return v;
    }

    std::optional<std::string> parse_string() {
        if (!consume('"')) return std::nullopt;
        std::string out;
        while (!eof()) {
            unsigned char c = static_cast<unsigned char>(text_[pos_++]);
            if (c == '"') return out;
            if (c < 0x20) return std::nullopt;  // raw control characters are invalid JSON
            if (c != '\\') {
                out += static_cast<char>(c);
                continue;
            }
            if (eof()) return std::nullopt;
            char e = text_[pos_++];
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    auto cp = hex4();
                    if (!cp) return std::nullopt;
                    if (*cp >= 0xD800 && *cp <= 0xDBFF) {  // surrogate pair
                        if (!literal("\\u")) return std::nullopt;
                        auto low = hex4();
                        if (!low || *low < 0xDC00 || *low > 0xDFFF) return std::nullopt;
                        *cp = 0x10000 + ((*cp - 0xD800) << 10) + (*low - 0xDC00);
                    } else if (*cp >= 0xDC00 && *cp <= 0xDFFF) {
                        return std::nullopt;
                    }
                    append_utf8(out, *cp);
                    break;
                }
                default: return std::nullopt;
            }
        }
        return std::nullopt;  // unterminated
    }
};

void write_string(std::string& out, const std::string& s) {
    out += '"';
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20 || c == 0x7F) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    out += '"';
}

void write_value(std::string& out, const Value& v);

void write_object(std::string& out, const Object& object) {
    out += '{';
    bool first = true;
    for (const auto& [key, value] : object) {
        if (!first) out += ',';
        first = false;
        write_string(out, key);
        out += ':';
        write_value(out, value);
    }
    out += '}';
}

void write_value(std::string& out, const Value& v) {
    if (v.is_null()) out += "null";
    else if (auto b = v.as_bool()) out += *b ? "true" : "false";
    else if (auto i = v.as_int()) out += std::to_string(*i);
    else if (auto s = v.as_string()) write_string(out, *s);
    else if (auto o = v.as_object()) write_object(out, *o);
}

}  // namespace

std::optional<Object> parse_object(std::string_view text) { return Parser(text).parse(); }

std::string serialize(const Object& object) {
    std::string out;
    write_object(out, object);
    return out;
}

std::optional<std::string> get_string(const Object& object, const std::string& key) {
    auto it = object.find(key);
    if (it == object.end()) return std::nullopt;
    if (auto s = it->second.as_string()) return *s;
    return std::nullopt;
}

std::optional<std::int64_t> get_int(const Object& object, const std::string& key) {
    auto it = object.find(key);
    if (it == object.end()) return std::nullopt;
    if (auto i = it->second.as_int()) return *i;
    return std::nullopt;
}

const Object* get_object(const Object& object, const std::string& key) {
    auto it = object.find(key);
    return it == object.end() ? nullptr : it->second.as_object();
}

}  // namespace bridge::json

namespace bridge::protocol {

json::Object response(std::int64_t id, const char* result, json::Object extra) {
    extra["id"] = json::Value(id);
    extra["ok"] = json::Value(std::string(result) == kSuccess || std::string(result) == kAuthorizing ||
                              std::string(result) == kReady);
    extra["result"] = json::Value(result);
    return extra;
}

json::Object event(const char* name, json::Object fields) {
    fields["event"] = json::Value(name);
    return fields;
}

std::optional<Request> parse_request(std::string_view line) {
    auto object = json::parse_object(line);
    if (!object) return std::nullopt;
    auto id = json::get_int(*object, "id");
    auto op = json::get_string(*object, "op");
    if (!id || !op || op->empty()) return std::nullopt;
    return Request{*id, *op, std::move(*object)};
}

std::optional<std::string> presence_field(const std::optional<std::string>& value) {
    if (!value) return std::nullopt;
    std::string out;
    std::size_t code_points = 0;
    const std::string& s = *value;
    for (std::size_t i = 0; i < s.size();) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        std::size_t len = c < 0x80 ? 1 : (c >> 5) == 0x6 ? 2 : (c >> 4) == 0xE ? 3 : (c >> 3) == 0x1E ? 4 : 0;
        if (len == 0 || i + len > s.size()) break;  // invalid UTF-8: stop at the last good character
        if (code_points == 128) break;
        out.append(s, i, len);
        i += len;
        ++code_points;
    }
    if (code_points < 2) return std::nullopt;
    return out;
}

}  // namespace bridge::protocol

namespace bridge::protocol {

std::optional<std::string> asset_key(const std::optional<std::string>& value) {
    static constexpr const char* kRegistered[] = {"ember-deck", "source-plex", "source-dab", "source-ytmusic",
                                                  "source-bluetooth"};
    if (!value) return std::nullopt;
    for (const char* key : kRegistered) {
        if (*value == key) return value;
    }
    return std::nullopt;
}

namespace {

bool ends_with(const std::string& text, const std::string& suffix) {
    return text.size() >= suffix.size() && text.compare(text.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::string lower(std::string text) {
    for (char& c : text) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return text;
}

bool private_or_local_host(const std::string& host) {
    if (host.empty() || host == "localhost" || host.find('.') == std::string::npos) return true;
    bool numeric = true;
    for (char c : host) {
        if (!((c >= '0' && c <= '9') || c == '.')) numeric = false;
    }
    if (numeric) return true;  // IPv4 literal (IPv6 literals are rejected via ':')
    static constexpr const char* kLocal[] = {".local", ".lan", ".home", ".internal", ".localdomain", ".arpa",
                                             ".localhost", ".plex.direct", ".onion", ".test", ".invalid"};
    for (const char* suffix : kLocal) {
        if (ends_with(host, suffix)) return true;
    }
    return false;
}

bool sensitive_query(const std::string& query) {
    static constexpr const char* kMarkers[] = {"token", "auth", "key", "sig", "secret", "passw", "session",
                                               "credential", "x-plex", "x-amz-", "x-goog-"};
    std::size_t start = 0;
    while (start <= query.size()) {
        std::size_t amp = query.find('&', start);
        if (amp == std::string::npos) amp = query.size();
        const std::string name = lower(query.substr(start, std::min(query.find('=', start), amp) - start));
        for (const char* marker : kMarkers) {
            if (name.find(marker) != std::string::npos) return true;
        }
        start = amp + 1;
    }
    return false;
}

}  // namespace

std::optional<std::string> external_image(const std::optional<std::string>& value, bool enabled) {
    static constexpr std::string_view kScheme = "https://";
    if (!enabled || !value || value->size() > 300 || value->rfind(kScheme, 0) != 0) return std::nullopt;
    const std::string& url = *value;
    for (char c : url) {
        const auto u = static_cast<unsigned char>(c);
        if (u <= 0x20 || u >= 0x7F || c == '#' || c == '\\') return std::nullopt;
    }
    const std::size_t end = url.find_first_of("/?", kScheme.size());
    const std::string authority = url.substr(kScheme.size(), end == std::string::npos ? std::string::npos
                                                                                      : end - kScheme.size());
    // '@': user info; ':': a port or an IPv6 literal.
    if (authority.find_first_of("@:") != std::string::npos) return std::nullopt;
    std::string host = lower(authority);
    if (!host.empty() && host.back() == '.') host.pop_back();
    if (private_or_local_host(host)) return std::nullopt;
    const std::size_t question = url.find('?');
    if (question != std::string::npos && sensitive_query(url.substr(question + 1))) return std::nullopt;
    if (lower(url).find("x-plex-token") != std::string::npos) return std::nullopt;
    return value;
}

std::optional<std::string> large_image(const std::optional<std::string>& value, bool external_enabled) {
    if (!value) return std::nullopt;
    if (auto key = asset_key(value)) return key;
    if (auto url = external_image(value, external_enabled)) return url;
    return std::string("ember-deck");  // rejected: fall back to the static artwork
}

std::optional<std::string> link_url(const std::optional<std::string>& value) {
    static constexpr std::string_view kScheme = "https://";
    if (!value || value->size() > 256 || value->rfind(kScheme, 0) != 0) return std::nullopt;
    const std::string& url = *value;
    for (char c : url) {
        const auto u = static_cast<unsigned char>(c);
        if (u <= 0x20 || u >= 0x7F || c == '\\') return std::nullopt;
    }
    const std::size_t end = url.find_first_of("/?#", kScheme.size());
    const std::string authority = url.substr(kScheme.size(), end == std::string::npos ? std::string::npos
                                                                                      : end - kScheme.size());
    if (authority.find('@') != std::string::npos) return std::nullopt;
    std::string host = authority.substr(0, authority.find(':'));
    while (!host.empty() && host.back() == '.') host.pop_back();
    if (host.empty() || host.front() == '.' || host.find('.') == std::string::npos) return std::nullopt;
    return value;
}

std::optional<std::uint64_t> timestamp_ms(const std::optional<std::int64_t>& value) {
    // 2001-09-09 .. 2286-11-20 in milliseconds.
    if (!value || *value < 1'000'000'000'000LL || *value >= 10'000'000'000'000LL) return std::nullopt;
    return static_cast<std::uint64_t>(*value);
}

}  // namespace bridge::protocol

namespace bridge::auth {

void wipe(std::string& text) {
    volatile char* data = text.data();
    for (std::size_t i = 0; i < text.size(); ++i) data[i] = 0;
    text.clear();
    text.shrink_to_fit();
}

void PendingVerifier::set(std::string verifier) {
    clear();
    value_ = std::move(verifier);
    active_ = true;
}

std::optional<std::string> PendingVerifier::take() {
    if (!active_) return std::nullopt;
    std::string copy = value_;
    clear();
    return copy;
}

void PendingVerifier::clear() {
    wipe(value_);
    active_ = false;
}

}  // namespace bridge::auth
