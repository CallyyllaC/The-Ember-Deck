// SDK-independent tests for the discord_bridge line protocol.
// Build: cmake -S . -B build && cmake --build build --target protocol_tests && ctest --test-dir build
#include "protocol.hpp"

#include <cstdio>
#include <string>

using namespace bridge;

static int failures = 0;
#define CHECK(cond)                                                         \
    do {                                                                    \
        if (!(cond)) {                                                      \
            std::fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
            ++failures;                                                     \
        }                                                                   \
    } while (0)

static void parses_requests() {
    auto r = protocol::parse_request(R"({"id":7,"op":"init","application_id":"123456789012345678"})");
    CHECK(r.has_value());
    CHECK(r->id == 7);
    CHECK(r->op == "init");
    CHECK(json::get_string(r->body, "application_id") == std::optional<std::string>("123456789012345678"));
}

static void rejects_malformed_lines() {
    CHECK(!protocol::parse_request("not json"));
    CHECK(!protocol::parse_request(R"({"op":"init"})"));                 // no id
    CHECK(!protocol::parse_request(R"({"id":"1","op":"init"})"));        // id not an int
    CHECK(!protocol::parse_request(R"({"id":1,"op":""})"));              // empty op
    CHECK(!protocol::parse_request(R"({"id":1,"op":"x"} trailing)"));
    CHECK(!protocol::parse_request(R"({"id":1,"op":"x","a":[1]})"));     // arrays unsupported
    CHECK(!protocol::parse_request(R"({"id":1.5,"op":"x"})"));           // floats unsupported
    CHECK(!protocol::parse_request(R"({"id":1,"op":"x","op":"y"})"));    // duplicate keys
    CHECK(!protocol::parse_request("{\"id\":1,\"op\":\"x\x01\"}"));    // raw control char
    CHECK(!protocol::parse_request(R"({"id":01,"op":"x"})"));            // leading zero
    CHECK(!protocol::parse_request(R"({"id":99999999999999999999,"op":"x"})"));  // overflow
}

static void nested_presence_and_unicode() {
    auto r = protocol::parse_request(
        R"({"id":2,"op":"set_presence","presence":{"type":"listening","details":"日本 🎵","state":"via Plex"}})");
    CHECK(r.has_value());
    const json::Object* p = json::get_object(r->body, "presence");
    CHECK(p != nullptr);
    CHECK(json::get_string(*p, "details") == std::optional<std::string>("\xE6\x97\xA5\xE6\x9C\xAC \xF0\x9F\x8E\xB5"));
}

static void serializes_single_lines() {
    json::Object o = protocol::response(3, protocol::kSuccess, {{"message", json::Value("line1\nline2 \"q\"")}});
    std::string line = json::serialize(o);
    CHECK(line.find('\n') == std::string::npos);
    auto back = json::parse_object(line);
    CHECK(back.has_value());
    CHECK(json::get_int(*back, "id") == std::optional<std::int64_t>(3));
    CHECK(json::get_string(*back, "message") == std::optional<std::string>("line1\nline2 \"q\""));
    CHECK(*back->at("ok").as_bool() == true);
    auto failed = json::parse_object(json::serialize(protocol::response(4, protocol::kTransient)));
    CHECK(*failed->at("ok").as_bool() == false);
    auto ev = json::parse_object(json::serialize(protocol::event("status", {{"state", json::Value("ready")}})));
    CHECK(json::get_string(*ev, "event") == std::optional<std::string>("status"));
    CHECK(!json::get_int(*ev, "id"));
}

static void presence_field_limits() {
    CHECK(!protocol::presence_field(std::nullopt));
    CHECK(!protocol::presence_field(std::string("x")));          // Discord minimum is 2
    CHECK(protocol::presence_field(std::string("ok")) == std::optional<std::string>("ok"));
    std::string longer(200, 'a');
    CHECK(protocol::presence_field(longer)->size() == 128);
    std::string cjk;
    for (int i = 0; i < 130; ++i) cjk += "\xE6\x97\xA5";       // 130 three-byte characters
    CHECK(protocol::presence_field(cjk)->size() == 128 * 3);     // trimmed on a character boundary
}

static void pkce_verifier_is_single_use_and_wiped() {
    bridge::auth::PendingVerifier pending;
    CHECK(!pending.active());
    CHECK(!pending.take());
    pending.set("verifier-abc");
    CHECK(pending.active());
    auto taken = pending.take();
    CHECK(taken && *taken == "verifier-abc");
    CHECK(!pending.active());
    CHECK(!pending.take());                  // usable once only
    pending.set("second");
    pending.clear();                         // abandoned flow
    CHECK(!pending.active() && !pending.take());
    std::string secret = "sensitive";
    bridge::auth::wipe(secret);
    CHECK(secret.empty());
}

static void assets_and_timestamps_are_allowlisted() {
    CHECK(protocol::asset_key(std::string("ember-deck")) == std::optional<std::string>("ember-deck"));
    CHECK(protocol::asset_key(std::string("source-ytmusic")));
    CHECK(!protocol::asset_key(std::string("https://i.ytimg.com/vi/x/hq.jpg")));
    CHECK(!protocol::asset_key(std::string("mp:external/abc")));
    CHECK(!protocol::asset_key(std::string("Ember-Deck")));
    CHECK(!protocol::asset_key(std::nullopt));
    CHECK(protocol::timestamp_ms(std::int64_t{1'800'000'000'000}) == std::optional<std::uint64_t>(1'800'000'000'000ULL));
    CHECK(!protocol::timestamp_ms(std::int64_t{1'800'000'000}));       // seconds, not ms
    CHECK(!protocol::timestamp_ms(std::int64_t{-5}));
    CHECK(!protocol::timestamp_ms(std::nullopt));
}

static void external_artwork_blocks_private_and_credential_urls() {
    const auto image = [](const char* url, bool enabled = true) {
        return protocol::large_image(std::string(url), enabled).value_or("");
    };
    const std::string fallback = "ember-deck";
    CHECK(image("source-dab") == "source-dab");                                  // registered assets still work
    CHECK(image("https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg") != fallback);
    CHECK(image("https://i.ytimg.com/vi/x/hq.jpg?sqp=-oaymwE&rs=AOn4CL") != fallback);  // ordinary query
    CHECK(image("https://radio.example.org/logo.png?size=192") != fallback);
    CHECK(image("https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg", false) == fallback);  // mode not "on"
    CHECK(image("http://radio.example.org/logo.png") == fallback);
    CHECK(image("file:///home/pi/a.png") == fallback);
    CHECK(image("https://user:pw@radio.example.org/logo.png") == fallback);
    CHECK(image("https://radio.example.org:8443/logo.png") == fallback);
    CHECK(image("https://radio.example.org/logo.png#x") == fallback);
    CHECK(image("https://192.168.1.20/logo.png") == fallback);
    CHECK(image("https://[fd00::1]/logo.png") == fallback);
    CHECK(image("https://localhost/logo.png") == fallback);
    CHECK(image("https://nas/logo.png") == fallback);
    CHECK(image("https://printer.local/logo.png") == fallback);
    CHECK(image("https://1-2-3-4.abc123.plex.direct/photo") == fallback);
    CHECK(image("https://cdn.example.org/a.png?X-Plex-Token=abc") == fallback);
    CHECK(image("https://cdn.example.org/a.png?access_token=abc") == fallback);
    CHECK(image("https://cdn.example.org/a.png?X-Amz-Signature=abc&X-Amz-Credential=x") == fallback);
    CHECK(image("https://cdn.example.org/a.png?Expires=1&Signature=abc") == fallback);
    CHECK(image(("https://cdn.example.org/" + std::string(300, 'a') + ".png").c_str()) == fallback);
    CHECK(!protocol::large_image(std::nullopt, true));
    CHECK(!protocol::asset_key(std::string("https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")));  // small image
}

static void image_links_are_https_page_urls_only() {
    const auto link = [](const std::string& url) { return protocol::link_url(url).value_or(""); };
    CHECK(link("https://awoo.ltd/projects/ember-deck") == "https://awoo.ltd/projects/ember-deck");
    CHECK(link("https://example.org/a?b=c#d") == "https://example.org/a?b=c#d");
    CHECK(link("https://example.org:8443/page") == "https://example.org:8443/page");
    CHECK(link("http://awoo.ltd/projects/ember-deck").empty());
    CHECK(link("javascript:alert(1)").empty());
    CHECK(link("https://user:pw@awoo.ltd/").empty());
    CHECK(link("https://awoo/").empty());
    CHECK(link("https://.ltd/").empty());
    CHECK(link("https:///path").empty());
    CHECK(link("https://awoo.ltd/a b").empty());
    CHECK(link("https://awoo.ltd/a\\b").empty());
    CHECK(link("https://awoo.ltd/" + std::string(239, 'a')).size() == 256);
    CHECK(link("https://awoo.ltd/" + std::string(240, 'a')).empty());
    CHECK(!protocol::link_url(std::nullopt));
}

int main() {
    external_artwork_blocks_private_and_credential_urls();
    image_links_are_https_page_urls_only();
    assets_and_timestamps_are_allowlisted();
    pkce_verifier_is_single_use_and_wiped();
    parses_requests();
    rejects_malformed_lines();
    nested_presence_and_unicode();
    serializes_single_lines();
    presence_field_limits();
    if (failures) {
        std::fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    std::puts("protocol_tests: all checks passed");
    return 0;
}
