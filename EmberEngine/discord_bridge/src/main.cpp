// discord_bridge: the only Ember Deck code that touches the Discord Social SDK.
//
// It contains no Ember Deck logic. rich_presence.py decides what to show,
// when to link, refresh or reconnect; this process only drives the SDK and
// reports results over newline-delimited JSON on stdin/stdout (see
// protocol.hpp and EmberEngine/discord_ipc.py). It opens no sockets of its
// own. When stdin closes (the worker died) it shuts down.
//
// stderr carries only fixed diagnostic lines and SDK error-level log lines.
// The worker keeps a bounded, redacted tail of it and reports it only when
// the bridge fails, so loader/Box64/SDK start-up errors can be diagnosed.
//
// Verified against discordpp.h from Discord Social SDK 1.10.19337. API used:
//   Client(), SetApplicationId, SetStatusChangedCallback, AddLogCallback,
//   GetStatus, UpdateToken, Connect, Disconnect, RefreshToken,
//   CreateAuthorizationCodeVerifier, Authorize, AbortAuthorize, GetToken,
//   GetDefaultPresenceScopes, UpdateRichPresence, ClearRichPresence,
//   RevokeToken, GetVersionMajor/Minor/Patch, and discordpp::RunCallbacks.
//
// Linking uses the desktop Public Client authorization-code + PKCE flow
// (Authorize, then GetToken). The console device flow (GetTokenFromDevice)
// is not used: on the Linux desktop SDK it aborts the process with
// "Check failed: CanAuthorizeDevice" (observed on a Raspberry Pi 5 with SDK
// 1.10.19337 under Box64), and discordpp.h directs desktop apps to Authorize.

#define DISCORDPP_IMPLEMENTATION
#include "discordpp.h"

#include "protocol.hpp"

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#else
#include <cerrno>
#include <unistd.h>
#endif

#include <chrono>
#include <csignal>
#include <cstdio>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

using bridge::json::Object;
using bridge::json::Value;
namespace protocol = bridge::protocol;

namespace {

volatile std::sig_atomic_t g_terminate = 0;
void on_signal(int) { g_terminate = 1; }

void emit(const Object& message) {
    std::string line = bridge::json::serialize(message);
    line += '\n';
    std::fwrite(line.data(), 1, line.size(), stdout);
    std::fflush(stdout);
}

void diagnostic(const std::string& text) {
    std::string line = "discord_bridge: " + text.substr(0, 512) + "\n";
    std::fwrite(line.data(), 1, line.size(), stderr);
    std::fflush(stderr);
}

// Map an SDK failure to the shared result vocabulary. SDK error text is not
// forwarded: it is not needed by the worker and could echo request data.
// Only an explicit HTTP rejection counts as "unauthenticated" (the worker
// unlinks on that); network trouble, a client that is not ready yet and
// retryable HTTP errors are "transient", so an outage never forces relinking.
const char* classify(const discordpp::ClientResult& result) {
    if (result.Successful()) return protocol::kSuccess;
    if (result.Retryable()) return protocol::kTransient;
    switch (result.Type()) {
        case discordpp::ErrorType::NetworkError:
        case discordpp::ErrorType::ClientNotReady:
        case discordpp::ErrorType::Disabled:
            return protocol::kTransient;
        case discordpp::ErrorType::HTTPError: {
            const int http = static_cast<int>(result.Status());
            if (http == 400 || http == 401 || http == 403) return protocol::kUnauthenticated;
            if (http == 408 || http == 429 || http >= 500) return protocol::kTransient;
            return protocol::kPermanent;
        }
        default:
            return protocol::kPermanent;
    }
}

const char* status_name(discordpp::Client::Status status) {
    switch (status) {
        case discordpp::Client::Status::Ready: return "ready";
        case discordpp::Client::Status::Reconnecting: return "reconnecting";
        case discordpp::Client::Status::Disconnected: return "disconnected";
        case discordpp::Client::Status::Disconnecting: return "disconnected";
        default: return "connecting";  // Connecting, Connected, HttpWait
    }
}

std::string sdk_version() {
    return std::to_string(discordpp::Client::GetVersionMajor()) + "." +
           std::to_string(discordpp::Client::GetVersionMinor()) + "." +
           std::to_string(discordpp::Client::GetVersionPatch());
}

// Reads stdin on its own thread so the main thread can pump SDK callbacks
// regularly on every platform. Only complete lines are handed over.
class LineReader {
public:
    void start() {
        auto state = state_;
        std::thread([state] { run(*state); }).detach();
    }

    // Moves complete lines into `out`. Returns false once input has closed
    // and every line has been taken.
    bool take(std::deque<std::string>& out) {
        std::lock_guard<std::mutex> lock(state_->mutex);
        while (!state_->lines.empty()) {
            out.push_back(std::move(state_->lines.front()));
            state_->lines.pop_front();
        }
        return !state_->closed;
    }

private:
    struct State {
        std::mutex mutex;
        std::deque<std::string> lines;
        bool closed = false;
    };
    std::shared_ptr<State> state_ = std::make_shared<State>();

    static void run(State& state) {
        std::string buffer;
        char chunk[4096];
        for (;;) {
#ifdef _WIN32
            const int n = _read(0, chunk, static_cast<unsigned>(sizeof chunk));
#else
            const ssize_t n = ::read(STDIN_FILENO, chunk, sizeof chunk);
            if (n < 0 && errno == EINTR) continue;
#endif
            if (n <= 0) break;
            buffer.append(chunk, static_cast<std::size_t>(n));
            std::size_t newline;
            std::lock_guard<std::mutex> lock(state.mutex);
            while ((newline = buffer.find('\n')) != std::string::npos) {
                state.lines.push_back(buffer.substr(0, newline));
                buffer.erase(0, newline + 1);
            }
            if (buffer.size() > 64 * 1024) buffer.clear();  // oversized line: drop it
        }
        std::lock_guard<std::mutex> lock(state.mutex);
        state.closed = true;
    }
};

class Bridge {
public:
    bool running() const { return running_; }

    void handle(const protocol::Request& request) {
        const std::string& op = request.op;
        if (op == "hello") {
            emit(protocol::response(request.id, protocol::kSuccess,
                                    {{"protocol", Value(protocol::kVersion)}, {"sdk_version", Value(sdk_version())}}));
        } else if (op == "init") {
            init(request);
        } else if (!client_) {
            emit(protocol::response(request.id, protocol::kSdkUnavailable, {{"message", Value("not initialised")}}));
        } else if (op == "set_token") {
            set_token(request);
        } else if (op == "refresh_token") {
            refresh_token(request);
        } else if (op == "begin_auth") {
            begin_auth(request);
        } else if (op == "cancel_auth") {
            cancel_auth();
            emit(protocol::response(request.id, protocol::kSuccess));
        } else if (op == "set_presence") {
            set_presence(request);
        } else if (op == "clear_presence") {
            client_->ClearRichPresence();
            emit(protocol::response(request.id, protocol::kSuccess));
        } else if (op == "revoke") {
            revoke(request);
        } else if (op == "shutdown") {
            shutdown(request.id);
        } else {
            emit(protocol::response(request.id, protocol::kPermanent, {{"message", Value("unknown op")}}));
        }
    }

    void shutdown(std::int64_t id) {
        if (client_) {
            cancel_auth();
            client_->ClearRichPresence();
            client_->Disconnect();
            // Give the SDK a moment to send the clear and close the socket.
            for (int i = 0; i < 50 && client_->GetStatus() != discordpp::Client::Status::Disconnected; ++i) {
                discordpp::RunCallbacks();
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
            discordpp::RunCallbacks();
            client_.reset();  // destroy the client before static teardown
        }
        if (id >= 0) emit(protocol::response(id, protocol::kSuccess));
        running_ = false;
    }

private:
    std::shared_ptr<discordpp::Client> client_;
    std::uint64_t application_id_ = 0;
    bool external_artwork_ = false;  // external large images allowed (worker's artwork_mode "on")
    bool running_ = true;
    // PKCE state for the authorization in progress. The verifier stays in this
    // process only and is wiped when exchanged, cancelled or abandoned.
    bridge::auth::PendingVerifier pending_verifier_;
    std::uint64_t auth_generation_ = 0;  // ignores callbacks from a cancelled attempt

    void init(const protocol::Request& request) {
        auto id_text = bridge::json::get_string(request.body, "application_id");
        std::uint64_t application_id = 0;
        try {
            if (id_text) application_id = std::stoull(*id_text);
        } catch (...) {
            application_id = 0;
        }
        if (application_id == 0) {
            emit(protocol::response(request.id, protocol::kPermanent, {{"message", Value("invalid application id")}}));
            return;
        }
        if (!client_) {
            client_ = std::make_shared<discordpp::Client>();
            if (!*client_) {
                diagnostic("Discord SDK client could not be created");
                client_.reset();
                emit(protocol::response(request.id, protocol::kPermanent, {{"message", Value("sdk client failed")}}));
                return;
            }
            // Errors only, to stderr (bounded and redacted by the worker).
            // The SDK writes no log file unless SetLogDir is called; it is not.
            client_->AddLogCallback(
                [](std::string message, discordpp::LoggingSeverity) { diagnostic("sdk: " + message); },
                discordpp::LoggingSeverity::Error);
            client_->SetStatusChangedCallback(
                [](discordpp::Client::Status status, discordpp::Client::Error error, int32_t detail) {
                    Object fields{{"state", Value(status_name(status))},
                                  {"error", Value(static_cast<std::int64_t>(error))},
                                  {"detail", Value(static_cast<std::int64_t>(detail))}};
                    // 4004 is the gateway's "authentication failed" close code.
                    if (detail == 4004) fields["result"] = Value(protocol::kUnauthenticated);
                    emit(protocol::event("status", std::move(fields)));
                });
        }
        application_id_ = application_id;
        auto external = request.body.find("external_artwork");
        external_artwork_ = external != request.body.end() && external->second.as_bool() &&
                            *external->second.as_bool();
        client_->SetApplicationId(application_id_);
        emit(protocol::response(request.id, protocol::kSuccess));
    }

    void set_token(const protocol::Request& request) {
        auto token = bridge::json::get_string(request.body, "access_token");
        if (!token || token->empty()) {
            emit(protocol::response(request.id, protocol::kPermanent, {{"message", Value("missing token")}}));
            return;
        }
        const std::int64_t id = request.id;
        std::weak_ptr<discordpp::Client> weak = client_;
        // UpdateToken may reconnect an already connected client by itself;
        // Connect is needed only when the client is not connected.
        client_->UpdateToken(discordpp::AuthorizationTokenType::Bearer, *token,
                             [id, weak](discordpp::ClientResult result) {
                                 auto client = weak.lock();
                                 if (result.Successful() && client) {
                                     const auto status = client->GetStatus();
                                     if (status == discordpp::Client::Status::Disconnected ||
                                         status == discordpp::Client::Status::Disconnecting) {
                                         client->Connect();
                                     }
                                 }
                                 emit(protocol::response(id, classify(result)));
                             });
    }

    static Object token_fields(const char* source, const std::string& access, const std::string& refresh,
                               int32_t expires_in, const std::string& scopes) {
        return {{"source", Value(source)},
                {"access_token", Value(access)},
                {"refresh_token", Value(refresh)},
                {"expires_in", Value(static_cast<std::int64_t>(expires_in))},
                {"scope", Value(scopes)}};
    }

    void refresh_token(const protocol::Request& request) {
        auto refresh = bridge::json::get_string(request.body, "refresh_token");
        if (!refresh || refresh->empty()) {
            emit(protocol::response(request.id, protocol::kPermanent, {{"message", Value("missing refresh token")}}));
            return;
        }
        // On success Discord invalidates the old access and refresh tokens and
        // returns a new pair; the worker persists both before using them.
        client_->RefreshToken(application_id_, *refresh,
                              [](discordpp::ClientResult result, std::string access, std::string new_refresh,
                                 discordpp::AuthorizationTokenType, int32_t expires_in, std::string scopes) {
                                  if (result.Successful()) {
                                      emit(protocol::event("tokens", token_fields("refresh", access, new_refresh,
                                                                                  expires_in, scopes)));
                                  } else {
                                      emit(protocol::event("auth_failed", {{"source", Value("refresh")},
                                                                           {"result", Value(classify(result))}}));
                                  }
                              });
        emit(protocol::response(request.id, protocol::kSuccess));
    }

    // Authorization failures. Aborted means the user (or cancel_auth) stopped
    // the flow; otherwise the general classification applies.
    static const char* classify_auth(const discordpp::ClientResult& result) {
        if (!result.Successful() && result.Type() == discordpp::ErrorType::Aborted) return protocol::kCancelled;
        return classify(result);
    }

    void auth_failed(std::uint64_t generation, const char* result) {
        if (generation != auth_generation_) return;  // superseded or cancelled
        pending_verifier_.clear();
        emit(protocol::event("auth_failed", {{"source", Value("authorize")}, {"result", Value(result)}}));
    }

    // Desktop Public Client sign-in: Authorize with a PKCE challenge, then
    // exchange the returned code with GetToken. The SDK presents Discord's
    // authorization page itself (Discord overlay, else the system browser) and
    // receives the redirect on its own loopback listener
    // (http://127.0.0.1/callback must be registered in the Developer Portal).
    // No client secret is involved. The code and verifier never leave this
    // process; only the resulting tokens are reported to the worker.
    void begin_auth(const protocol::Request& request) {
        cancel_auth();  // at most one attempt at a time
        const std::uint64_t generation = ++auth_generation_;
        discordpp::AuthorizationCodeVerifier verifier = client_->CreateAuthorizationCodeVerifier();
        pending_verifier_.set(verifier.Verifier());

        discordpp::AuthorizationArgs args{};
        args.SetClientId(application_id_);
        // Default presence scopes only (openid sdk.social_layer_presence):
        // no communication, messaging or voice permissions are requested.
        args.SetScopes(discordpp::Client::GetDefaultPresenceScopes());
        args.SetCodeChallenge(verifier.Challenge());
        // State is left unset: the SDK generates and checks it (CSRF).

        client_->Authorize(std::move(args), [this, generation](discordpp::ClientResult result, std::string code,
                                                               std::string redirect_uri) {
            if (generation != auth_generation_) {
                bridge::auth::wipe(code);
                return;
            }
            if (!result.Successful()) {
                bridge::auth::wipe(code);
                auth_failed(generation, classify_auth(result));
                return;
            }
            auto code_verifier = pending_verifier_.take();
            if (!code_verifier) {
                bridge::auth::wipe(code);
                auth_failed(generation, protocol::kPermanent);
                return;
            }
            client_->GetToken(application_id_, code, *code_verifier, redirect_uri,
                              [this, generation](discordpp::ClientResult token_result, std::string access,
                                                 std::string refresh, discordpp::AuthorizationTokenType,
                                                 int32_t expires_in, std::string scopes) {
                                  if (generation != auth_generation_) return;
                                  if (token_result.Successful()) {
                                      emit(protocol::event("tokens", token_fields("authorize", access, refresh,
                                                                                  expires_in, scopes)));
                                  } else {
                                      auth_failed(generation, classify_auth(token_result));
                                  }
                                  bridge::auth::wipe(access);
                                  bridge::auth::wipe(refresh);
                              });
            bridge::auth::wipe(*code_verifier);
            bridge::auth::wipe(code);
        });
        emit(protocol::response(request.id, protocol::kAuthorizing));
    }

    // Stops a pending authorization. Per discordpp.h, AbortAuthorize does not
    // close a browser window the SDK already opened.
    void cancel_auth() {
        if (!pending_verifier_.active()) return;
        ++auth_generation_;
        pending_verifier_.clear();
        client_->AbortAuthorize();
    }

    void set_presence(const protocol::Request& request) {
        const Object* presence = bridge::json::get_object(request.body, "presence");
        if (!presence) {
            emit(protocol::response(request.id, protocol::kPermanent, {{"message", Value("missing presence")}}));
            return;
        }
        // A partial activity: the SDK fills in the application name and ID.
        discordpp::Activity activity;
        auto type = bridge::json::get_string(*presence, "type");
        activity.SetType(type && *type == "playing" ? discordpp::ActivityTypes::Playing
                                                    : discordpp::ActivityTypes::Listening);
        activity.SetDetails(protocol::presence_field(bridge::json::get_string(*presence, "details")));
        activity.SetState(protocol::presence_field(bridge::json::get_string(*presence, "state")));
        // Images: registered asset keys only (asset_key drops anything else).
        // Large image: registered asset, or (artwork_mode "on" only) an
        // external URL passing external_image(); a rejected URL becomes
        // the static "ember-deck" asset. Either image may carry a click-through
        // link (large_url/small_url) that passes link_url(); else no link.
        auto large = protocol::large_image(bridge::json::get_string(*presence, "large_image"), external_artwork_);
        auto small = protocol::asset_key(bridge::json::get_string(*presence, "small_image"));
        if (large || small) {
            discordpp::ActivityAssets assets;
            if (large) {
                assets.SetLargeImage(large);
                assets.SetLargeText(protocol::presence_field(bridge::json::get_string(*presence, "large_text")));
                assets.SetLargeUrl(protocol::link_url(bridge::json::get_string(*presence, "large_url")));
            }
            if (small) {
                assets.SetSmallImage(small);
                assets.SetSmallText(protocol::presence_field(bridge::json::get_string(*presence, "small_text")));
                assets.SetSmallUrl(protocol::link_url(bridge::json::get_string(*presence, "small_url")));
            }
            activity.SetAssets(assets);
        }
        // Media timing (ms since epoch): start alone shows elapsed time,
        // start + end shows track progress.
        if (auto start = protocol::timestamp_ms(bridge::json::get_int(*presence, "start_ms"))) {
            discordpp::ActivityTimestamps timestamps;
            timestamps.SetStart(*start);
            auto end = protocol::timestamp_ms(bridge::json::get_int(*presence, "end_ms"));
            if (end && *end > *start) timestamps.SetEnd(*end);
            activity.SetTimestamps(timestamps);
        }
        const std::int64_t id = request.id;
        client_->UpdateRichPresence(std::move(activity), [id](discordpp::ClientResult result) {
            emit(protocol::response(id, classify(result)));
        });
    }

    void revoke(const protocol::Request& request) {
        auto token = bridge::json::get_string(request.body, "token");
        if (!token || token->empty()) {
            emit(protocol::response(request.id, protocol::kPermanent, {{"message", Value("missing token")}}));
            return;
        }
        const std::int64_t id = request.id;
        client_->RevokeToken(application_id_, *token, [id](discordpp::ClientResult result) {
            emit(protocol::response(id, classify(result)));
        });
    }
};

}  // namespace

int main() {
    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);
#ifdef SIGPIPE
    std::signal(SIGPIPE, SIG_IGN);
#endif
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif

    Bridge bridge;
    LineReader reader;
    reader.start();
    std::deque<std::string> lines;
    bool input_open = true;

    // Callbacks run only from discordpp::RunCallbacks(), on this thread, so
    // all SDK use and all stdout writes stay single-threaded.
    while (bridge.running() && !g_terminate) {
        input_open = reader.take(lines);
        while (!lines.empty() && bridge.running()) {
            const std::string line = std::move(lines.front());
            lines.pop_front();
            if (auto request = protocol::parse_request(line)) {
                bridge.handle(*request);
            } else {
                emit(protocol::event("protocol_error", {{"message", Value("malformed request")}}));
            }
        }
        discordpp::RunCallbacks();
        if (!input_open) break;  // the worker went away: shut down below
        std::this_thread::sleep_for(std::chrono::milliseconds(16));
    }
    if (bridge.running()) bridge.shutdown(-1);
    return 0;
}
