import Foundation
import Observation

/// What the PC agent reports in `/status`.
struct PCStatus: Decodable, Sendable {
    let ok: Bool
    let name: String
    let cpu: Double
    let ramUsedGb: Double
    let ramTotalGb: Double
    let uptimeS: Int
    let running: [String]
    let windows: [OpenWindow]
    let publicIp: String?
    let wolPort: Int?
    let wolMapping: String?
    let micMuted: Bool?
    let cameraEnabled: Bool?
    let idleS: Int?
    let afkMinutes: Int?
    let ntfyTopic: String?
}

struct OpenWindow: Decodable, Hashable, Sendable {
    let app: String
    let title: String
    let hwnd: Int
}

/// Talks to the agent running on the PC (agent.py) and sends Wake-on-LAN packets.
@MainActor
@Observable
final class PCClient {
    private(set) var settings: ConnectionSettings
    private(set) var status: PCStatus?
    private(set) var apps: [String] = []
    /// The base URL that last answered (home Wi-Fi or the internet address).
    private(set) var activeEndpoint: String
    /// Last connection error per endpoint, shown when the PC looks offline.
    private(set) var lastErrors: [String: String] = [:]
    var message = ""

    var online: Bool { status != nil }
    var isAway: Bool { activeEndpoint != settings.homeURL }

    init() {
        let loaded = ConnectionSettings.load()
        settings = loaded
        activeEndpoint = loaded.homeURL
    }

    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        // Generous: a round trip over the internet can take a second or two.
        config.timeoutIntervalForRequest = 6
        config.timeoutIntervalForResource = 10
        return URLSession(configuration: config)
    }()

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func reloadSettings() {
        settings = ConnectionSettings.load()
        activeEndpoint = settings.homeURL
    }

    func isRunning(_ app: String) -> Bool {
        status?.running.contains(app) ?? false
    }

    // MARK: - Status

    func refresh() async {
        struct AppList: Decodable { let apps: [String] }
        // Try every address at the same time; the first one that answers wins.
        // (Home Wi-Fi fails slowly on cellular, so trying in order would stall.)
        let endpoints = settings.endpoints
        let snapshot = settings
        let session = self.session
        let decoder = self.decoder
        var errors: [String: String] = [:]
        var winner: (endpoint: String, status: PCStatus)?

        await withTaskGroup(of: (String, Result<PCStatus, Error>).self) { group in
            for endpoint in endpoints {
                group.addTask {
                    do {
                        let s: PCStatus = try await Self.fetch("/status", endpoint: endpoint, settings: snapshot,
                                                               session: session, decoder: decoder)
                        return (endpoint, .success(s))
                    } catch {
                        return (endpoint, .failure(error))
                    }
                }
            }
            for await (endpoint, result) in group {
                switch result {
                case .success(let s):
                    if winner == nil {
                        winner = (endpoint, s)
                        group.cancelAll()
                    }
                case .failure(let error):
                    errors[endpoint] = Self.describe(error)
                }
            }
        }

        if let winner {
            activeEndpoint = winner.endpoint
            status = winner.status
            lastErrors = [:]
            if let ip = winner.status.publicIp, !ip.isEmpty, ip != settings.lastPublicIP {
                UserDefaults.standard.set(ip, forKey: SettingsKey.lastPublicIP)
                settings.lastPublicIP = ip
            }
            if apps.isEmpty {
                if let list: AppList = try? await get("/apps") {
                    apps = list.apps
                }
            }
        } else {
            status = nil
            lastErrors = errors
        }
    }

    private static func describe(_ error: Error) -> String {
        if let e = error as? URLError {
            switch e.code {
            case .timedOut: return "timed out"
            case .cannotConnectToHost: return "connection refused"
            case .cannotFindHost: return "address not found"
            case .notConnectedToInternet: return "no internet"
            case .appTransportSecurityRequiresSecureConnection: return "blocked by iOS (ATS)"
            default: return e.localizedDescription
            }
        }
        return error.localizedDescription
    }

    // MARK: - Actions

    /// Sends the magic packet on the home network and, if known, to the home
    /// public IP on the port the router forwards to the PC (works from anywhere).
    func wake() {
        var targets = [
            WakeOnLAN.Target(address: settings.broadcast, port: 9),
            WakeOnLAN.Target(address: settings.host, port: 9),
            WakeOnLAN.Target(address: "255.255.255.255", port: 9),
        ]
        if !settings.lastPublicIP.isEmpty {
            targets.append(WakeOnLAN.Target(address: settings.lastPublicIP, port: UInt16(clamping: settings.wolPort)))
        }
        do {
            let sent = try WakeOnLAN.send(mac: settings.mac, targets: targets)
            if settings.lastPublicIP.isEmpty {
                message = "Wake packet sent on the home network. (No home public IP known yet, so it can't be sent over the internet until the app has seen the PC online once.)"
            } else {
                message = "Wake packet sent to home network and \(settings.lastPublicIP):\(settings.wolPort). The PC should turn on in a few seconds."
            }
            _ = sent
        } catch {
            message = "Wake failed: \(error.localizedDescription)"
        }
    }

    func sleep() async {
        do {
            _ = try await post("/sleep") as Ack
            message = "PC is going to sleep."
            status = nil
        } catch {
            message = "Sleep failed: \(error.localizedDescription)"
        }
    }

    func restart() async {
        do {
            _ = try await post("/restart") as Ack
            message = "PC is restarting. It should be back in about a minute."
            status = nil
        } catch {
            message = "Restart failed: \(error.localizedDescription)"
        }
    }

    func closeAll() async {
        struct Closed: Decodable { let ok: Bool; let closed: Int }
        do {
            let r: Closed = try await post("/close-all")
            message = "Closing \(r.closed) app\(r.closed == 1 ? "" : "s")."
        } catch {
            message = "Close all failed: \(error.localizedDescription)"
        }
    }

    func launch(_ app: String) async {
        let path = "/launch/" + (app.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? app)
        do {
            _ = try await post(path) as Ack
            message = "Opened \(app)."
        } catch {
            message = "Could not open \(app): \(error.localizedDescription)"
        }
    }

    func close(_ app: String) async {
        let path = "/close/" + (app.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? app)
        do {
            _ = try await post(path) as Ack
            message = "Closed \(app)."
        } catch {
            message = "Could not close \(app): \(error.localizedDescription)"
        }
    }

    func close(window: OpenWindow) async {
        do {
            _ = try await post("/close-window/\(window.hwnd)") as Ack
            message = "Closed \(window.title)."
        } catch {
            message = "Could not close that window: \(error.localizedDescription)"
        }
    }

    func fetchStats() async -> PCStats? {
        try? await get("/stats")
    }

    func toggleMic() async {
        struct Mic: Decodable { let ok: Bool; let micMuted: Bool }
        do {
            let r: Mic = try await post("/mic/toggle")
            message = r.micMuted ? "Microphone muted." : "Microphone on."
            await refresh()
        } catch {
            message = "Microphone failed: \(error.localizedDescription)"
        }
    }

    func toggleCamera() async {
        struct Cam: Decodable { let ok: Bool; let cameraEnabled: Bool }
        do {
            let r: Cam = try await post("/camera/toggle")
            message = r.cameraEnabled ? "Camera on." : "Camera off for all apps."
            await refresh()
        } catch {
            message = "Camera failed: \(error.localizedDescription)"
        }
    }

    func testNotification() async {
        do {
            _ = try await post("/notify-test") as Ack
            message = "Test notification sent to ntfy."
        } catch {
            message = "Notification failed: \(error.localizedDescription)"
        }
    }

    // MARK: - HTTP

    private struct Ack: Decodable { let ok: Bool }

    private struct HTTPError: LocalizedError {
        let status: Int
        let body: String
        var errorDescription: String? { "HTTP \(status) \(body)" }
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await Self.fetch(path, endpoint: activeEndpoint, settings: settings, session: session, decoder: decoder)
    }

    private func post<T: Decodable>(_ path: String) async throws -> T {
        try await Self.fetch(path, endpoint: activeEndpoint, settings: settings, session: session,
                             decoder: decoder, method: "POST")
    }

    private static func fetch<T: Decodable>(_ path: String, endpoint: String, settings: ConnectionSettings,
                                            session: URLSession, decoder: JSONDecoder,
                                            method: String = "GET") async throws -> T {
        guard let base = settings.baseURL(for: endpoint), let url = URL(string: path, relativeTo: base) else {
            throw URLError(.badURL)
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue(settings.token, forHTTPHeaderField: "X-Token")
        let (data, response) = try await session.data(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            throw HTTPError(status: code, body: String(data: data, encoding: .utf8) ?? "")
        }
        return try decoder.decode(T.self, from: data)
    }
}
