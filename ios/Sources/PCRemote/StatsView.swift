import SwiftUI

/// Everything the agent reports in `/stats`.
struct PCStats: Decodable, Sendable {
    struct Disk: Decodable, Sendable, Hashable {
        let name: String
        let usedGb: Double
        let totalGb: Double
    }
    struct GPU: Decodable, Sendable {
        let name: String
        let util: Double
        let memUsedGb: Double
        let memTotalGb: Double
        let tempC: Double
    }
    struct Proc: Decodable, Sendable, Hashable {
        let name: String
        let cpu: Double
        let memMb: Int
    }

    let name: String
    let uptimeS: Int
    let cpu: Double
    let cpuPerCore: [Double]
    let cpuCores: Int
    let cpuThreads: Int
    let cpuMhz: Int
    let ramUsedGb: Double
    let ramTotalGb: Double
    let swapUsedGb: Double
    let swapTotalGb: Double
    let disks: [Disk]
    let gpu: GPU?
    let netUpKbps: Double
    let netDownKbps: Double
    let netSentGb: Double
    let netRecvGb: Double
    let processCount: Int
    let topCpu: [Proc]
    let topMem: [Proc]
}

struct StatsView: View {
    let client: PCClient
    @State private var stats: PCStats?

    var body: some View {
        List {
            if let s = stats {
                Section("CPU") {
                    MeterRow(title: "Total", value: s.cpu, max: 100, text: "\(Int(s.cpu.rounded()))%")
                    LabeledContent("Cores", value: "\(s.cpuCores) cores / \(s.cpuThreads) threads")
                    LabeledContent("Clock", value: "\(s.cpuMhz) MHz")
                    ForEach(Array(s.cpuPerCore.enumerated()), id: \.offset) { i, pct in
                        MeterRow(title: "Core \(i + 1)", value: pct, max: 100, text: "\(Int(pct.rounded()))%")
                            .font(.footnote)
                    }
                }
                Section("Memory") {
                    MeterRow(title: "RAM", value: s.ramUsedGb, max: s.ramTotalGb,
                             text: "\(fmt(s.ramUsedGb)) / \(fmt(s.ramTotalGb)) GB")
                    MeterRow(title: "Swap", value: s.swapUsedGb, max: max(s.swapTotalGb, 0.1),
                             text: "\(fmt(s.swapUsedGb)) / \(fmt(s.swapTotalGb)) GB")
                }
                if let g = s.gpu {
                    Section("GPU") {
                        LabeledContent("Model", value: g.name)
                        MeterRow(title: "Usage", value: g.util, max: 100, text: "\(Int(g.util.rounded()))%")
                        MeterRow(title: "VRAM", value: g.memUsedGb, max: g.memTotalGb,
                                 text: "\(fmt(g.memUsedGb)) / \(fmt(g.memTotalGb)) GB")
                        LabeledContent("Temperature", value: "\(Int(g.tempC.rounded())) °C")
                    }
                }
                Section("Disks") {
                    ForEach(s.disks, id: \.self) { d in
                        MeterRow(title: d.name, value: d.usedGb, max: d.totalGb,
                                 text: "\(fmt(d.usedGb)) / \(fmt(d.totalGb)) GB")
                    }
                }
                Section("Network") {
                    LabeledContent("Download", value: rate(s.netDownKbps))
                    LabeledContent("Upload", value: rate(s.netUpKbps))
                    LabeledContent("Received since boot", value: "\(fmt(s.netRecvGb)) GB")
                    LabeledContent("Sent since boot", value: "\(fmt(s.netSentGb)) GB")
                }
                Section("System") {
                    LabeledContent("Uptime", value: uptime(s.uptimeS))
                    LabeledContent("Processes", value: "\(s.processCount)")
                }
                Section("Top by CPU") {
                    ForEach(s.topCpu, id: \.self) { p in
                        LabeledContent(p.name, value: "\(Int(p.cpu.rounded()))%")
                    }
                }
                Section("Top by memory") {
                    ForEach(s.topMem, id: \.self) { p in
                        LabeledContent(p.name, value: "\(p.memMb) MB")
                    }
                }
            } else {
                Section {
                    Text(client.online ? "Loading…" : "PC is offline.")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("PC stats")
        .task {
            while !Task.isCancelled {
                stats = await client.fetchStats()
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    private func fmt(_ v: Double) -> String {
        v.formatted(.number.precision(.fractionLength(0...1)))
    }

    private func rate(_ kbps: Double) -> String {
        kbps >= 1024 ? "\(fmt(kbps / 1024)) MB/s" : "\(Int(kbps.rounded())) KB/s"
    }

    private func uptime(_ seconds: Int) -> String {
        let h = seconds / 3600, m = (seconds % 3600) / 60
        return h >= 24 ? "\(h / 24)d \(h % 24)h \(m)m" : "\(h)h \(m)m"
    }
}

private struct MeterRow: View {
    let title: String
    let value: Double
    let max: Double
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title)
                Spacer()
                Text(text).foregroundStyle(.secondary)
            }
            ProgressView(value: min(Swift.max(value, 0), max), total: max)
                .tint(value / max > 0.85 ? .red : value / max > 0.6 ? .orange : .green)
        }
    }
}
