import Foundation

/// User-editable connection settings, persisted in UserDefaults.
/// Defaults match the PC this app was built for.
enum SettingsKey {
    static let host = "pc.host"
    static let port = "pc.port"
    static let awayURL = "pc.awayURL"
    static let mac = "pc.mac"
    static let broadcast = "pc.broadcast"
    static let token = "pc.token"
    static let wolPort = "pc.wolPort"
    static let lastPublicIP = "pc.lastPublicIP"
}

enum SettingsDefaults {
    static let host = "192.168.100.85"                                   // home Wi-Fi
    static let port = 8765
    static let awayURL = "https://desktop-3iem09n.tail0070df.ts.net"     // Tailscale Funnel, works from anywhere
    static let mac = "D8:5E:D3:D3:52:32"
    static let broadcast = "192.168.100.255"
    static let token = Secrets.defaultToken
    static let wolPort = 40009                                           // UDP port the router forwards to the PC
}

struct ConnectionSettings: Sendable {
    var host: String
    var port: Int
    var awayURL: String
    var mac: String
    var broadcast: String
    var token: String
    var wolPort: Int
    /// Home's public IP, learned from the agent the last time it answered.
    var lastPublicIP: String

    static func load() -> ConnectionSettings {
        let d = UserDefaults.standard
        return ConnectionSettings(
            host: d.string(forKey: SettingsKey.host) ?? SettingsDefaults.host,
            port: d.object(forKey: SettingsKey.port) as? Int ?? SettingsDefaults.port,
            awayURL: d.string(forKey: SettingsKey.awayURL) ?? SettingsDefaults.awayURL,
            mac: d.string(forKey: SettingsKey.mac) ?? SettingsDefaults.mac,
            broadcast: d.string(forKey: SettingsKey.broadcast) ?? SettingsDefaults.broadcast,
            token: d.string(forKey: SettingsKey.token) ?? SettingsDefaults.token,
            wolPort: d.object(forKey: SettingsKey.wolPort) as? Int ?? SettingsDefaults.wolPort,
            lastPublicIP: d.string(forKey: SettingsKey.lastPublicIP) ?? ""
        )
    }

    var homeURL: String { "http://\(host):\(port)" }

    /// Base URLs to try, home first. Both are tried at once; the first to answer wins.
    var endpoints: [String] {
        var list = [homeURL]
        let away = awayURL.trimmingCharacters(in: .whitespacesAndNewlines)
        if !away.isEmpty {
            list.append(away.hasSuffix("/") ? String(away.dropLast()) : away)
        }
        return list
    }

    func baseURL(for endpoint: String) -> URL? {
        URL(string: endpoint)
    }
}
