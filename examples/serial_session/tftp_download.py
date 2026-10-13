"""Distribute a file to a BusyBox Linux device using TFTP + a serial shell.

pip install -e ".[serial,tftp]"
python examples/serial_session/tftp_download.py firmware.bin --serial COM3 --host 192.168.1.10

The device must already have a logged-in POSIX shell, BusyBox tftp and sha256sum.
--host is the PC's IPv4 address reachable from the board. UDP 69 and the server's
transfer ports must be reachable. This downloads and checks a file; it does not
flash firmware. U-Boot and other command dialects need their own explicit script.
"""
import argparse
import hashlib
from pathlib import Path
import re
import shlex
import uuid

import redpymake as rpm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--target", default="/tmp/firmware.bin")
    args = parser.parse_args()
    source = args.file.resolve(strict=True)
    source.name.encode("ascii")  # TFTP protocol filename; local directory may be Unicode.
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    token = "RPM_TFTP_" + uuid.uuid4().hex
    target = shlex.quote(args.target)
    command = (
        f"tftp -g -r {shlex.quote(source.name)} -l {target} {shlex.quote(args.host)}; "
        'rc=$?; '
        f'printf "\\n{token}_RC=%s\\n" "$rc"; '
        f'if [ "$rc" -eq 0 ]; then sha256sum {target}; fi\r'
    )
    with rpm.tftpd(source.parent, host=args.host) as server, rpm.serial(
        args.serial, baudrate=args.baudrate,
    ) as uart:
        # run().wait() searches from the pre-write cursor. An anchored unique
        # marker cannot be satisfied by the shell echo of the command itself.
        result = uart.run(command)
        status = result.wait(re.compile(rf"^{token}_RC=(\d+)\s*$"), timeout=30)
        if status.text.strip() != token + "_RC=0":
            raise rpm.TransferError(f"device TFTP command failed: {status.text}",
                                    source=str(source), target=args.target)
        server.wait(source.name, timeout=5)
        result.wait(re.compile(rf"^{digest}\s+\*?{re.escape(args.target)}\s*$"), timeout=10)
        print(f"Verified {source.name} -> {args.target} ({digest})")


if __name__ == "__main__":
    main()
