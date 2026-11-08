import Foundation

/// Sends a Wake-on-LAN "magic packet" over UDP using BSD sockets.
enum WakeOnLAN {
    struct Target: Sendable {
        let address: String
        let port: UInt16
    }

    struct WakeError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Sends the magic packet to every target (LAN broadcast, the PC's LAN IP,
    /// and the home public IP + forwarded port when away). Succeeds if at least
    /// one send works; returns the addresses it reached.
    @discardableResult
    static func send(mac: String, targets: [Target]) throws -> [String] {
        let packet = try magicPacket(mac: mac)
        var lastError: WakeError?
        var sent: [String] = []
        for target in targets {
            do {
                try sendUDP(packet, to: target.address, port: target.port)
                sent.append("\(target.address):\(target.port)")
            } catch let error as WakeError {
                lastError = error
            }
        }
        if sent.isEmpty {
            throw lastError ?? WakeError(message: "No targets")
        }
        return sent
    }

    static func magicPacket(mac: String) throws -> [UInt8] {
        let parts = mac.split(whereSeparator: { $0 == ":" || $0 == "-" })
        guard parts.count == 6 else {
            throw WakeError(message: "MAC must look like D8:5E:D3:D3:52:32")
        }
        let bytes = try parts.map { part -> UInt8 in
            guard let value = UInt8(part, radix: 16) else {
                throw WakeError(message: "Bad MAC byte: \(part)")
            }
            return value
        }
        var packet = [UInt8](repeating: 0xFF, count: 6)
        for _ in 0..<16 {
            packet.append(contentsOf: bytes)
        }
        return packet
    }

    private static func sendUDP(_ packet: [UInt8], to address: String, port: UInt16) throws {
        let fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard fd >= 0 else {
            throw WakeError(message: "socket() failed: \(String(cString: strerror(errno)))")
        }
        defer { close(fd) }

        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        guard inet_pton(AF_INET, address, &addr.sin_addr) == 1 else {
            throw WakeError(message: "Bad IP address: \(address)")
        }

        let sent = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                packet.withUnsafeBytes { buf in
                    sendto(fd, buf.baseAddress, buf.count, 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        guard sent == packet.count else {
            throw WakeError(message: "send to \(address) failed: \(String(cString: strerror(errno)))")
        }
    }
}
