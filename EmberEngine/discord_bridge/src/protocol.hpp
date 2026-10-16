// Minimal JSON for the discord_bridge line protocol.
//
// The protocol only needs objects whose values are null, booleans, 64-bit
// integers, strings or nested objects, so that is all this parser accepts.
// Arrays and floating-point numbers are rejected rather than half-supported.
// It has no dependency on the Discord Social SDK and is unit-tested on its own.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <variant>

namespace bridge::json {

struct Value;
using Object = std::map<std::string, Value>;

struct Value {
    std::variant<std::monostate, bool, std::int64_t, std::string, std::shared_ptr<Object>> data;

    Value() = default;
    Value(std::nullptr_t) {}
    Value(bool b) : data(b) {}
    Value(int i) : data(static_cast<std::int64_t>(i)) {}
    Value(std::int64_t i) : data(i) {}
    Value(const char* s) : data(std::string(s)) {}
    Value(std::string s) : data(std::move(s)) {}
    Value(Object o) : data(std::make_shared<Object>(std::move(o))) {}

    bool is_null() const { return std::holds_alternative<std::monostate>(data); }
    const std::string* as_string() const { return std::get_if<std::string>(&data); }
    const std::int64_t* as_int() const { return std::get_if<std::int64_t>(&data); }
    const bool* as_bool() const { return std::get_if<bool>(&data); }
    const Object* as_object() const {
        auto p = std::get_if<std::shared_ptr<Object>>(&data);
        return p ? p->get() : nullptr;
    }
};

// Parse one complete JSON object. Returns nullopt on any syntax error,
// trailing content, nesting deeper than 8 levels, or unsupported type.
std::optional<Object> parse_object(std::string_view text);

// Serialise to a single line (no embedded newlines; strings escaped).
std::string serialize(const Object& object);

// Convenience accessors returning nullopt when absent or of another type.
std::optional<std::string> get_string(const Object& object, const std::string& key);
std::optional<std::int64_t> get_int(const Object& object, const std::string& key);
const Object* get_object(const Object& object, const std::string& key);

}  // namespace bridge::json

namespace bridge::protocol {

inline constexpr int kVersion = 1;

// Result vocabulary shared with discord_ipc.py.
inline constexpr const char* kSuccess = "success";
inline constexpr const char* kSdkUnavailable = "sdk_unavailable";
inline constexpr const char* kUnauthenticated = "unauthenticated";
inline constexpr const char* kAuthorizing = "authorizing";
inline constexpr const char* kReady = "ready";
inline constexpr const char* kDisconnected = "disconnected";
inline constexpr const char* kTransient = "transient";
inline constexpr const char* kPermanent = "permanent";
inline constexpr const char* kCancelled = "cancelled";

json::Object response(std::int64_t id, const char* result, json::Object extra = {});
json::Object event(const char* name, json::Object fields = {});

// Validate a request line: an object with integer "id" and string "op".
struct Request {
    std::int64_t id;
    std::string op;
    json::Object body;
};
std::optional<Request> parse_request(std::string_view line);

// Discord limits details/state to 2..128 characters; returns nullopt when
// the field should be omitted. Counts UTF-8 code points, trims on a boundary.
std::optional<std::string> presence_field(const std::optional<std::string>& value);

// Rich Presence image keys must be one of the assets registered in the
// Discord Developer Portal. Anything else (including any URL) is dropped, so
// no dynamic or external artwork can reach Discord through the bridge.
std::optional<std::string> asset_key(const std::optional<std::string>& value);

// An external large image, accepted only when the worker enabled external
// artwork at init (artwork_mode "on"): https, at most 300 characters (the
// SDK's image-field limit), no user info, port, fragment or IP literal, not a
// local name (localhost, single label, .local/.lan/.plex.direct/...), and no
// query parameter whose name suggests credentials or signed access (token,
// auth, key, sig, secret, session, x-plex, x-amz-, x-goog-, ...). Mirrors
// EmberEngine/artwork_policy.py; anything else is dropped.
std::optional<std::string> external_image(const std::optional<std::string>& value, bool enabled);

// The large image: a registered asset key, else an acceptable external URL,
// else (a rejected URL) the registered "ember-deck" asset. The small image
// only ever accepts registered keys (asset_key).
std::optional<std::string> large_image(const std::optional<std::string>& value, bool external_enabled);

// A click-through link for an image (ActivityAssets large_url/small_url):
// "https://", at most 256 characters (the SDK's limit), printable ASCII
// without spaces or backslashes, no user info, and a dotted host name.
// Mirrors EmberEngine/presence_model.link_url; anything else is dropped,
// leaving the image without a link.
std::optional<std::string> link_url(const std::optional<std::string>& value);

// Activity timestamps are milliseconds since the Unix epoch; implausible
// values (seconds, negatives, far future) are dropped.
std::optional<std::uint64_t> timestamp_ms(const std::optional<std::int64_t>& value);

}  // namespace bridge::protocol

namespace bridge::auth {

// Holds the PKCE code verifier between Client::Authorize and Client::GetToken.
// The verifier never leaves the bridge (not sent to the worker, not logged)
// and is overwritten in memory as soon as it is used or abandoned.
class PendingVerifier {
public:
    PendingVerifier() = default;
    PendingVerifier(const PendingVerifier&) = delete;
    PendingVerifier& operator=(const PendingVerifier&) = delete;
    ~PendingVerifier() { clear(); }

    void set(std::string verifier);
    bool active() const { return active_; }
    // Returns the verifier once and clears the stored copy.
    std::optional<std::string> take();
    void clear();

private:
    std::string value_;
    bool active_ = false;
};

// Overwrites a string's characters before releasing it.
void wipe(std::string& text);

}  // namespace bridge::auth
