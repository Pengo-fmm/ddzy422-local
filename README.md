# DDZY422-D2 local reader (no cloud)

Read the **Solarman / Chint DDZY422-D2** single-phase smart meter **locally**, over
your LAN, without the Solarman cloud, without an account, and without an appId.

## Why this exists

The DDZY422-D2 has an *integrated* Solarman WiFi logger. Every attempt in the
community to read it locally with the usual **Solarman V5 protocol (TCP 8899)**
fails: the meter only ever answers with an "acknowledge / still processing" frame
and never returns Modbus data. See e.g.
[davidrapan/ha-solarman#187](https://github.com/davidrapan/ha-solarman/issues/187),
[StephanJoubert/home_assistant_solarman#586](https://github.com/StephanJoubert/home_assistant_solarman/issues/586)
and various forum threads — all dead ends.

The trick is to use a **different, lower-level channel** the HF/Solarman WiFi
module exposes: the **AT-command passthrough over UDP port 48899**. You wrap a
normal Modbus-RTU frame in `AT+INVDATA=<len>,<hex>` and the module relays it to
the internal meter and returns the Modbus response. That path works.

Tested on logger module firmware **`LSW3_15_E030_1.05`** (HF...-YZ). Behaviour may
differ on other revisions.

## Usage

```bash
python3 ddzy422_reader.py --host 192.168.1.50 --serial 1234567890
# or JSON:
python3 ddzy422_reader.py --host 192.168.1.50 --serial 1234567890 --json
```

`--serial` is the logger serial (printed on the stick / shown in the SOLARMAN app).
Find the meter's IP from your router's DHCP list, or with the discovery handshake:

```bash
# broadcast/unicast this to UDP 48899 and the module answers "ip,mac,serial"
printf 'WIFIKIT-214028-READ' | nc -u -w2 <meter-ip> 48899
```

Example output:

```
voltage_v            = 227.8
current_a            = 0.62
active_power_w       = -138      # >0 importing from grid, <0 exporting to grid
reactive_power_var   = 40
power_factor         = -0.222
frequency_hz         = 50.03
import_total_kwh     = 7568.73
export_total_kwh     = 16446.34
```

## The protocol, in detail

The bits that took reverse-engineering (so you don't have to):

1. **Handshake** (UDP 48899): send `WIFIKIT-214028-READ`; the module replies
   `ip,mac,serial`; reply `+ok`.
2. **Request**: `AT+INVDATA=<len>,<modbus-rtu-frame-as-hex>\n`
   (standard Modbus RTU, function code 3, with CRC16).
3. **Response**: `+ok=<hex-ASCII>\r\n\r\n`, where the payload is the Modbus reply
   as hex **with `0x10` used as an escape/separator byte** — strip `0x10`, then
   `bytes.fromhex()`.
4. The channel is **flaky** (`+ERR=-1`, `+ok=no data`, or silence happen): send a
   short "drain" read of the socket before each request and **retry** a few times.
5. ⚠️ **Always validate the Modbus CRC of the response.** The AT channel will hand you
   *corrupted frames that still look structurally valid* (right slave id, right function
   code, right length). Without the CRC check you silently get garbage values — typically
   near `2**31/100` (≈21,474,836) — and if you feed those into a `total_increasing` sensor
   (Home Assistant, etc.) they destroy your energy statistics permanently. Ask me how I know.
6. ⚠️ **Check that the reply has the number of registers you asked for** (byte count
   = 2 × count). Now and then the module answers a request with the reply to a different
   one: valid frame, valid CRC, wrong content. On my meter the energy request (`0x000E`,
   20 registers) got the 14-register instantaneous block back ten times in two weeks, at
   one poll every 15 s. Decoded as energy, that reads import = voltage ÷ 10 (23.03 kWh at
   230.3 V). On a `total_increasing` sensor the drop is ignored or taken as a reset, and
   the climb back to the real value is counted as energy. The reader rejects these
   replies and asks again. If you publish the totals, also check that they never go down.

### Register map (from the official iGEN doc `HT-YF2018-0929-01`)

Function code 3. Two useful blocks:

**Instantaneous** `0x0000`–`0x000D`:

| Reg | Field | Type / scale |
|-----|-------|--------------|
| 0x0001 | Voltage | u16, ÷10 → V |
| 0x0002–0x0003 | Live current | **sign-magnitude** 32-bit, ÷1000 → A |
| 0x0006–0x0007 | Active power (total) | **sign-magnitude** 32-bit → W (>0 import, <0 export) |
| 0x0008–0x0009 | Reactive power | sign-magnitude 32-bit → var |
| 0x000C | Power factor | **sign-magnitude 16-bit**, ÷1000 (⚠ NOT two's complement) |
| 0x000D | Frequency | u16, ÷100 → Hz |

**Energy** `0x000E`–`0x0021` (all u32, ÷100 → kWh):

| Reg | Field |
|-----|-------|
| 0x000E–0x000F | Forward active total (**import**) |
| 0x0018–0x0019 | Reverse active total (**export**) |
| 0x0010–0x0017 | Forward tariff bands (sharp/peak/flat/valley) |
| 0x001A–0x0021 | Reverse tariff bands |

> ⚠️ **Sign is sign-magnitude, not two's complement.** The top bit is the sign;
> the remaining bits are the magnitude. Getting this wrong makes the power factor
> and any exporting/negative power read as garbage.

## Notes

- This does **not** interrupt the cloud: the logger stays in its normal mode and
  keeps reporting to Solarman. You just poll it locally in parallel.
- The AT channel serves roughly one client at a time — don't run two pollers
  against the same logger simultaneously.
- If you'd rather go fully local and cut the cloud, the logger's hidden web UI
  (`http://<meter-ip>/config_hide.html`, default `admin`/`admin`) has a
  `yz_tmode` = `throughput` option that turns port 8899 into a clean raw
  TCP↔serial bridge (native Modbus RTU) — but that **disables cloud reporting**.

## License

MIT — see [LICENSE](LICENSE).

Register semantics from the manufacturer's Modbus document; this project only
documents and uses them.
