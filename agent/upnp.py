"""Tiny UPnP IGD client (no dependencies).

Used by the PC Remote agent to ask the home router for its public IP and to
open a UDP port that forwards Wake-on-LAN packets from the internet to this
PC. Only the handful of SOAP calls we need are implemented.
"""

import re
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SSDP_ADDR = ("239.255.255.250", 1900)
SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


class UPnPError(Exception):
    pass


def lan_ip() -> str:
    """The local address used to reach the internet (the LAN side of the router)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 53))
        return s.getsockname()[0]
    finally:
        s.close()


def discover(timeout: float = 3.0, gateway_ip: str | None = None) -> list[str]:
    """Return LOCATION URLs of UPnP root devices on the LAN.

    Searches for any root device (the Huawei HG8245Q ignores the IGD-specific
    search target) and sends both multicast and a unicast probe to the router.
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: upnp:rootdevice\r\n\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    locations: list[str] = []
    try:
        try:
            local = lan_ip()
            sock.bind((local, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
        except OSError:
            pass
        sock.sendto(msg, SSDP_ADDR)
        if gateway_ip:
            sock.sendto(msg, (gateway_ip, 1900))
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                loc = m.group(1).decode()
                if loc not in locations:
                    locations.append(loc)
    finally:
        sock.close()
    return locations


class Gateway:
    def __init__(self, location: str):
        self.location = location
        xml = urllib.request.urlopen(location, timeout=5).read()
        root = ET.fromstring(xml)
        ns = {"d": "urn:schemas-upnp-org:device-1-0"}
        self.friendly_name = (root.findtext(".//d:friendlyName", namespaces=ns) or "").strip()
        self.manufacturer = (root.findtext(".//d:manufacturer", namespaces=ns) or "").strip()
        self.model = (root.findtext(".//d:modelName", namespaces=ns) or "").strip()
        base = root.findtext("d:URLBase", namespaces=ns) or location
        self.service_type = None
        self.control_url = None
        for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            st = (svc.findtext("d:serviceType", namespaces=ns) or "").strip()
            if st in SERVICE_TYPES:
                self.service_type = st
                self.control_url = urllib.parse.urljoin(base, (svc.findtext("d:controlURL", namespaces=ns) or "").strip())
                break
        if not self.control_url:
            raise UPnPError("gateway has no WAN connection service")

    def _call(self, action: str, args: dict[str, str]) -> dict[str, str]:
        body_args = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{self.service_type}">{body_args}</u:{action}>'
            "</s:Body></s:Envelope>"
        ).encode()
        req = urllib.request.Request(
            self.control_url, data=body,
            headers={"Content-Type": 'text/xml; charset="utf-8"',
                     "SOAPAction": f'"{self.service_type}#{action}"'},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5).read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            code = re.search(r"<errorCode>(\d+)</errorCode>", detail)
            desc = re.search(r"<errorDescription>(.*?)</errorDescription>", detail)
            raise UPnPError(f"{action} failed: {code.group(1) if code else e.code} "
                            f"{desc.group(1) if desc else ''}".strip())
        out: dict[str, str] = {}
        for el in ET.fromstring(resp).iter():
            if el.tag.startswith("New") and el.text is not None:
                out[el.tag] = el.text
        return out

    def external_ip(self) -> str:
        return self._call("GetExternalIPAddress", {}).get("NewExternalIPAddress", "")

    def add_port_mapping(self, ext_port: int, proto: str, int_ip: str, int_port: int,
                         description: str, lease: int = 0) -> None:
        self._call("AddPortMapping", {
            "NewRemoteHost": "",
            "NewExternalPort": str(ext_port),
            "NewProtocol": proto,
            "NewInternalPort": str(int_port),
            "NewInternalClient": int_ip,
            "NewEnabled": "1",
            "NewPortMappingDescription": description,
            "NewLeaseDuration": str(lease),
        })

    def delete_port_mapping(self, ext_port: int, proto: str) -> None:
        self._call("DeletePortMapping", {
            "NewRemoteHost": "",
            "NewExternalPort": str(ext_port),
            "NewProtocol": proto,
        })

    def get_port_mapping(self, ext_port: int, proto: str) -> dict[str, str] | None:
        try:
            return self._call("GetSpecificPortMappingEntry", {
                "NewRemoteHost": "",
                "NewExternalPort": str(ext_port),
                "NewProtocol": proto,
            })
        except UPnPError:
            return None


def find_gateway(gateway_ip: str | None = None) -> Gateway | None:
    for loc in discover(gateway_ip=gateway_ip):
        try:
            return Gateway(loc)
        except (UPnPError, OSError, ET.ParseError):
            continue
    return None


if __name__ == "__main__":
    gw = find_gateway()
    if gw is None:
        print("No UPnP gateway found")
    else:
        print("gateway:", gw.friendly_name, "|", gw.manufacturer, "|", gw.model)
        print("service:", gw.service_type)
        print("external IP:", gw.external_ip())
