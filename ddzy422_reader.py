#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Local reader for the Solarman/Chint DDZY422-D2 single-phase smart meter.

The DDZY422-D2 has an *integrated* Solarman WiFi logger. The usual Solarman V5
protocol (TCP port 8899) does NOT work with it: the meter only ever answers with
an "acknowledge / still processing" frame and never returns Modbus data. This is
why the HA community had marked it as "not readable locally".

This script uses a different, lower-level channel exposed by the HF/Solarman WiFi
module: the AT command passthrough over UDP port 48899. It wraps a normal Modbus
RTU frame in `AT+INVDATA=<len>,<hex>` and the module relays it to the internal
meter, returning the Modbus response. That path works.

Tested on module firmware LSW3_15_E030_1.05 (HF...-YZ). Behaviour may differ on
other firmware revisions. No cloud, no Solarman account, no appId required.

Register map: official iGEN document "DDZY422-D2 ...-modbus" (HT-YF2018-0929-01).

Usage:
    python3 ddzy422_reader.py --host 192.168.1.50 --serial 1234567890
    python3 ddzy422_reader.py --host 192.168.1.50 --serial 1234567890 --json
"""
import argparse
import json
import socket
import struct
import sys
import time

HANDSHAKE = b"WIFIKIT-214028-READ"   # magic that makes the module reply with ip,mac,serial


def crc16(data: bytes) -> bytes:
    """Standard Modbus CRC16 (little-endian, poly 0xA001)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def s32(hi: int, lo: int) -> int:
    """32-bit sign-MAGNITUDE (top bit = sign), as used by current / power / reactive."""
    v = (hi << 16) | lo
    return -(v & 0x7FFFFFFF) if v & 0x80000000 else v


def sm16(v: int) -> int:
    """16-bit sign-MAGNITUDE (top bit = sign), as used by the power factor. NOT two's complement."""
    return -(v & 0x7FFF) if v & 0x8000 else v


class DDZY422:
    def __init__(self, host: str, serial: int, port: int = 48899, slave: int = 1, timeout: int = 8):
        self.addr = (host, port)
        self.slave = slave
        self.timeout = timeout
        self.sock = None

    def __enter__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(6)
        self.sock.sendto(HANDSHAKE, self.addr)
        self.sock.recvfrom(1024)          # ip,mac,serial
        self.sock.sendto(b"+ok", self.addr)
        time.sleep(0.4)
        return self

    def __exit__(self, *a):
        if self.sock:
            self.sock.close()

    def _drain(self):
        # the AT channel interleaves unsolicited frames; flush before each request
        self.sock.settimeout(0.4)
        try:
            while True:
                self.sock.recvfrom(4096)
        except socket.timeout:
            pass
        self.sock.settimeout(self.timeout)

    def read(self, address: int, count: int, tries: int = 5):
        """Read `count` holding registers (fc 3) starting at `address`. Returns list[int] or None.

        The channel is flaky (+ERR / +ok=no data happen); retry a few times.
        """
        frame = bytes([self.slave, 3, address >> 8, address & 0xFF, count >> 8, count & 0xFF])
        rtu = frame + crc16(frame)
        cmd = b"AT+INVDATA=" + str(len(rtu)).encode() + b"," + rtu.hex().encode() + b"\n"
        for _ in range(tries):
            self._drain()
            self.sock.sendto(cmd, self.addr)
            try:
                d, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            if d.startswith(b"+ok=") and b"no data" not in d and b"ERR" not in d:
                try:
                    # response is hex-ASCII, with 0x10 used as an escape/separator byte -> strip it
                    raw = bytes.fromhex(d[4:].replace(b"\x10", b"").rstrip(b"\r\n").decode())
                except ValueError:
                    continue
                if len(raw) < 5 or raw[0] != self.slave or raw[1] != 3:
                    continue
                # The reply must carry the registers that were asked for. Now and then
                # the module answers with the reply to a different request (seen: the
                # 14-register instantaneous block returned for the energy request). The
                # frame and its CRC are valid, the content is not: decoded as energy it
                # gives import = voltage / 10.
                if raw[2] != 2 * count:
                    time.sleep(0.5)
                    continue
                end = 3 + raw[2]
                if len(raw) < end + 2:
                    continue
                # Validate the Modbus CRC. The AT channel is flaky and WILL hand you
                # corrupted frames that otherwise look structurally fine; without this
                # check you get garbage values (e.g. near 2**31/100), which silently
                # destroy any total_increasing statistics downstream. Learned the hard way.
                if crc16(raw[:end]) != raw[end:end + 2]:
                    continue
                body = raw[3:end]
                return [struct.unpack(">H", body[i:i + 2])[0] for i in range(0, len(body), 2)]
            time.sleep(1.0)
        return None

    def read_all(self):
        """Read the instantaneous + energy blocks and decode them."""
        var = self.read(0x0000, 0x0E)     # instantaneous variables 0x0000..0x000D
        eng = self.read(0x000E, 0x14)     # energy block 0x000E..0x0021 (import/export totals + tariff bands)
        if not var or not eng:
            return None
        return {
            "voltage_v":        round(var[1] / 10, 1),          # 0x0001
            "current_a":        round(s32(var[2], var[3]) / 1000, 3),   # 0x0002-3 (signed: +/-)
            "active_power_w":   s32(var[6], var[7]),            # 0x0006-7  >0 import, <0 export
            "reactive_power_var": s32(var[8], var[9]),          # 0x0008-9
            "power_factor":     round(sm16(var[12]) / 1000, 3),  # 0x000C (sign-magnitude!)
            "frequency_hz":     round(var[13] / 100, 2),        # 0x000D
            "import_total_kwh": round((eng[0] << 16 | eng[1]) / 100, 2),    # 0x000E-F (forward active total)
            "export_total_kwh": round((eng[10] << 16 | eng[11]) / 100, 2),  # 0x0018-19 (reverse active total)
        }


def main():
    ap = argparse.ArgumentParser(description="Read a Solarman/Chint DDZY422-D2 meter locally (no cloud).")
    ap.add_argument("--host", required=True, help="IP of the meter's WiFi logger on your LAN")
    ap.add_argument("--serial", required=True, type=int, help="logger serial number (printed on the stick / in the app)")
    ap.add_argument("--slave", type=int, default=1, help="Modbus slave id (default 1)")
    ap.add_argument("--json", action="store_true", help="output as JSON")
    args = ap.parse_args()

    with DDZY422(args.host, args.serial, slave=args.slave) as m:
        data = m.read_all()
    if not data:
        print("No response from meter (the AT channel is flaky; try again).", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for k, v in data.items():
            print(f"{k:20s} = {v}")


if __name__ == "__main__":
    main()
