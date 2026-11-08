import SwiftUI

struct ContentView: View {
    @State private var client = PCClient()
    @State private var showSettings = false
    @State private var confirmCloseAll = false
    @State private var confirmRestart = false

    var body: some View {
        NavigationStack {
            List {
                statusSection
                powerSection
                appsSection
                windowsSection
                if !client.message.isEmpty {
                    Section {
                        Text(client.message)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("PC Remote")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showSettings, onDismiss: { client.reloadSettings() }) {
                SettingsView(client: client)
            }
            .task {
                // Poll the PC every 3 seconds while the screen is visible.
                while !Task.isCancelled {
                    await client.refresh()
                    try? await Task.sleep(for: .seconds(3))
                }
            }
        }
    }

    private var statusSection: some View {
        Section {
            HStack {
                Circle()
                    .fill(client.online ? Color.green : Color.red)
                    .frame(width: 12, height: 12)
                Text(client.online ? "Online" : "Offline")
                    .font(.headline)
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(client.status?.name ?? client.settings.host)
                    if client.online {
                        Text(client.isAway ? "via internet" : "home Wi-Fi")
                            .foregroundStyle(.tertiary)
                    }
                }
                .foregroundStyle(.secondary)
                .font(.footnote)
            }
            if !client.online, !client.lastErrors.isEmpty {
                ForEach(client.lastErrors.keys.sorted(), id: \.self) { host in
                    Text("\(host): \(client.lastErrors[host] ?? "")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            if let s = client.status {
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                    StatTile(title: "CPU", value: "\(Int(s.cpu.rounded()))%", icon: "cpu")
                    StatTile(title: "RAM", value: "\(formatGB(s.ramUsedGb)) / \(formatGB(s.ramTotalGb)) GB", icon: "memorychip")
                    StatTile(title: "On for", value: formatUptime(s.uptimeS), icon: "clock")
                    StatTile(title: "Idle", value: formatIdle(s.idleS ?? 0), icon: "person.fill.questionmark")
                }
                .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
                .listRowBackground(Color.clear)
                if let muted = s.micMuted {
                    Button {
                        Task { await client.toggleMic() }
                    } label: {
                        HStack {
                            Label("Microphone", systemImage: muted ? "mic.slash.fill" : "mic.fill")
                                .foregroundStyle(muted ? .red : .green)
                            Spacer()
                            Text(muted ? "Off" : "On")
                                .foregroundStyle(.secondary)
                            Image(systemName: muted ? "circle" : "checkmark.circle.fill")
                                .foregroundStyle(muted ? Color.secondary : Color.green)
                        }
                    }
                }
                if let cam = s.cameraEnabled {
                    Button {
                        Task { await client.toggleCamera() }
                    } label: {
                        HStack {
                            Label("Camera", systemImage: cam ? "video.fill" : "video.slash.fill")
                                .foregroundStyle(cam ? .green : .red)
                            Spacer()
                            Text(cam ? "On" : "Off")
                                .foregroundStyle(.secondary)
                            Image(systemName: cam ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(cam ? Color.green : Color.secondary)
                        }
                    }
                }
                NavigationLink {
                    StatsView(client: client)
                } label: {
                    Label("PC stats", systemImage: "chart.bar.xaxis")
                }
            }
        }
    }

    private var powerSection: some View {
        Section("Power") {
            VStack(spacing: 12) {
                HStack(spacing: 12) {
                    PowerButton(title: "Turn On", icon: "power", tint: .green) {
                        client.wake()
                    }
                    PowerButton(title: "Sleep", icon: "moon.zzz.fill", tint: .indigo) {
                        Task { await client.sleep() }
                    }
                    .disabled(!client.online)
                }
                HStack(spacing: 12) {
                    PowerButton(title: "Close all", icon: "xmark.square.fill", tint: .orange) {
                        confirmCloseAll = true
                    }
                    .disabled(!client.online)
                    PowerButton(title: "Restart", icon: "arrow.clockwise", tint: .red) {
                        confirmRestart = true
                    }
                    .disabled(!client.online)
                }
            }
            .listRowInsets(EdgeInsets())
            .listRowBackground(Color.clear)
            .confirmationDialog("Close every open app on the PC?", isPresented: $confirmCloseAll, titleVisibility: .visible) {
                Button("Close all apps", role: .destructive) {
                    Task { await client.closeAll() }
                }
            } message: {
                Text("Unsaved work in those apps will be lost.")
            }
            .confirmationDialog("Restart the PC?", isPresented: $confirmRestart, titleVisibility: .visible) {
                Button("Restart", role: .destructive) {
                    Task { await client.restart() }
                }
            } message: {
                Text("All apps close and the PC reboots. It comes back in about a minute.")
            }
        }
    }

    private var appsSection: some View {
        Section("Apps") {
            if client.apps.isEmpty {
                Text(client.online ? "Loading…" : "Turn on the PC to see apps.")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(client.apps, id: \.self) { app in
                    Button {
                        Task { await client.launch(app) }
                    } label: {
                        HStack {
                            Label(app, systemImage: AppIcons.symbol(for: app))
                            Spacer()
                            if client.isRunning(app) {
                                Text("Running")
                                    .font(.caption)
                                    .foregroundStyle(.green)
                                Circle()
                                    .fill(Color.green)
                                    .frame(width: 8, height: 8)
                            }
                        }
                    }
                    .disabled(!client.online)
                    .swipeActions(edge: .trailing) {
                        if client.isRunning(app) {
                            Button(role: .destructive) {
                                Task { await client.close(app) }
                            } label: {
                                Label("End task", systemImage: "xmark.circle.fill")
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var windowsSection: some View {
        if let windows = client.status?.windows, !windows.isEmpty {
            Section("Open on the PC") {
                ForEach(windows, id: \.self) { window in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(window.app)
                            .font(.subheadline.weight(.medium))
                        Text(window.title)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .swipeActions(edge: .trailing) {
                        Button(role: .destructive) {
                            Task { await client.close(window: window) }
                        } label: {
                            Label("End task", systemImage: "xmark.circle.fill")
                        }
                    }
                }
            }
        }
    }

    private func formatGB(_ value: Double) -> String {
        value.formatted(.number.precision(.fractionLength(0...1)))
    }

    private func formatIdle(_ seconds: Int) -> String {
        if seconds < 60 { return "active" }
        let minutes = seconds / 60
        return minutes >= 60 ? "\(minutes / 60)h \(minutes % 60)m" : "\(minutes) min"
    }

    private func formatUptime(_ seconds: Int) -> String {
        let hours = seconds / 3600
        let minutes = (seconds % 3600) / 60
        if hours >= 24 {
            return "\(hours / 24)d \(hours % 24)h \(minutes)m"
        }
        return "\(hours)h \(minutes)m"
    }
}

private struct StatTile: View {
    let title: String
    let value: String
    let icon: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(title, systemImage: icon)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
    }
}

private struct PowerButton: View {
    let title: String
    let icon: String
    let tint: Color
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 28, weight: .semibold))
                Text(title)
                    .font(.headline)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 20)
        }
        .buttonStyle(.borderedProminent)
        .tint(tint)
    }
}

private enum AppIcons {
    static func symbol(for app: String) -> String {
        let name = app.lowercased()
        if name.contains("study") { return "book.fill" }
        if name.contains("coding") { return "chevron.left.forwardslash.chevron.right" }
        if name.contains("discord") { return "bubble.left.and.bubble.right.fill" }
        if name.contains("chrome") || name.contains("edge") { return "globe" }
        if name.contains("minecraft") { return "cube.fill" }
        if name.contains("steam") { return "gamecontroller.fill" }
        if name.contains("spotify") { return "music.note" }
        return "app.fill"
    }
}

struct SettingsView: View {
    let client: PCClient
    @Environment(\.dismiss) private var dismiss
    @AppStorage(SettingsKey.host) private var host = SettingsDefaults.host
    @AppStorage(SettingsKey.awayURL) private var awayURL = SettingsDefaults.awayURL
    @AppStorage(SettingsKey.port) private var port = SettingsDefaults.port
    @AppStorage(SettingsKey.mac) private var mac = SettingsDefaults.mac
    @AppStorage(SettingsKey.broadcast) private var broadcast = SettingsDefaults.broadcast
    @AppStorage(SettingsKey.token) private var token = SettingsDefaults.token
    @AppStorage(SettingsKey.wolPort) private var wolPort = SettingsDefaults.wolPort
    @AppStorage(SettingsKey.lastPublicIP) private var lastPublicIP = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("PC") {
                    TextField("Home Wi-Fi address", text: $host)
                        .keyboardType(.decimalPad)
                    TextField("Port", value: $port, format: .number)
                        .keyboardType(.numberPad)
                    SecureField("Token", text: $token)
                }
                Section {
                    TextField("https://…ts.net", text: $awayURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Internet address")
                } footer: {
                    Text("Tailscale Funnel address of the PC. Used automatically when you're not on home Wi-Fi.")
                }
                Section {
                    TextField("MAC address", text: $mac)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                    TextField("Broadcast address", text: $broadcast)
                        .keyboardType(.decimalPad)
                    TextField("Wake port on router", value: $wolPort, format: .number)
                        .keyboardType(.numberPad)
                    LabeledContent("Home public IP", value: lastPublicIP.isEmpty ? "not learned yet" : lastPublicIP)
                } header: {
                    Text("Turn On (Wake-on-LAN)")
                } footer: {
                    Text("The public IP is learned from the PC each time it's online, and used to wake it from outside.")
                }
                Section {
                    if let topic = client.status?.ntfyTopic, !topic.isEmpty {
                        LabeledContent("ntfy topic", value: topic)
                            .textSelection(.enabled)
                        LabeledContent("Notify after idle", value: "\(client.status?.afkMinutes ?? 0) min")
                        Button("Send test notification") {
                            Task { await client.testNotification() }
                        }
                    } else {
                        Text(client.online ? "No ntfy topic configured on the PC." : "Connect to the PC to see the notification settings.")
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Idle notifications")
                } footer: {
                    Text("Install the free ntfy app from the App Store, subscribe to this topic, and you'll get a push notification when the PC is on but unused.")
                }
                Section {
                    Button("Reset to defaults", role: .destructive) {
                        host = SettingsDefaults.host
                        awayURL = SettingsDefaults.awayURL
                        port = SettingsDefaults.port
                        mac = SettingsDefaults.mac
                        broadcast = SettingsDefaults.broadcast
                        token = SettingsDefaults.token
                        wolPort = SettingsDefaults.wolPort
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
