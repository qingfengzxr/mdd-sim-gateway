"""Exercise the rendered inbound pickup block against real Asterisk and fake SIP peers.

Run only in a disposable engine container with --network none and no production
mounts. Set MDD_TEST_ISOLATED_ASTERISK=1. Requires Asterisk and Jinja2; never calls
an operator or real number. All SIP/AMI sockets bind to the container loopback.
"""
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[2] / "engine" / "templates"


def check(mode, voicemail=False, original=False):
    with tempfile.TemporaryDirectory(prefix="mdd-fanout-") as folder:
        root = Path(folder)
        for directory in ("run", "log", "spool", "data"):
            (root / directory).mkdir()
        (root / "asterisk.conf").write_text(f"""[directories]
astetcdir => {root}
astmoddir => /usr/lib/asterisk/modules
astvarlibdir => /var/lib/asterisk
astdbdir => {root}/data
astdatadir => /var/lib/asterisk
astspooldir => {root}/spool
astrundir => {root}/run
astlogdir => {root}/log
[options]
verbose = 4
""")
        (root / "modules.conf").write_text("[modules]\nautoload=yes\n")
        (root / "logger.conf").write_text("[logfiles]\nconsole=notice,warning,error,verbose\n")
        (root / "manager.conf").write_text(
            "[general]\nenabled=yes\nbindaddr=127.0.0.1\nport=5038\n"
            "[fixture]\nsecret=fixture-only\nread=all\nwrite=all\n")
        (root / "pjsip.conf").write_text("""[transport-udp]
type=transport
protocol=udp
bind=127.0.0.1:5060
[webrtc]
type=endpoint
transport=transport-udp
context=fixture
disallow=all
allow=ulaw
aors=webrtc
direct_media=no
[webrtc]
type=aor
contact=sip:browser@127.0.0.1:5090
contact=sip:mobile@127.0.0.1:5091
""")
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                          trim_blocks=True, lstrip_blocks=True)
        rendered = env.get_template("extensions.conf.j2").render(
            webrtc_enable=True, webrtc_user="webrtc", vm_enabled=voicemail,
            vm_ring_seconds=3, ring_timeout=3, vm_max_seconds=3)
        block = rendered[rendered.index("same => n,Set(BROWSER_PICKUP_DEADLINE="):
                         rendered.index("; i (invalid)")]
        block = block.replace("${EPOCH}+60", "${EPOCH}+3")
        if original:
            block = block.replace("Dial(${BROWSER_CONTACTS},", "Dial(PJSIP/webrtc,")
        (root / "extensions.conf").write_text(
            "[fixture]\nexten => s,1,NoOp(Synthetic incoming call)\n" + block +
            "\nexten => h,1,NoOp(RESULT=${DIALSTATUS}:${HANGUPCAUSE})\nsame => n,Hangup()\n"
            "[mdd_voicemail]\nexten => vm,1,NoOp(UNEXPECTED_VOICEMAIL)\nsame => n,Hangup()\n")
        stop = threading.Event()
        events = {5090: [], 5091: []}
        errors = []
        sockets = []
        threads = []

        def serve(udp, port):
            invite = None

            def reply(message, code, body=""):
                phrases = {180: "Ringing", 200: "OK", 404: "Not Found",
                           487: "Request Terminated", 603: "Decline"}
                headers = []
                for name in ("Via", "From", "To", "Call-ID", "CSeq"):
                    value = next(line for line in message.split("\r\n")
                                 if line.lower().startswith(name.lower() + ":"))
                    if name == "To" and ";tag=" not in value:
                        value += f";tag=fixture-{port}"
                    headers.append(value)
                if body:
                    headers += [f"Contact: <sip:fixture@127.0.0.1:{port}>",
                                "Content-Type: application/sdp"]
                response = "\r\n".join([f"SIP/2.0 {code} {phrases[code]}", *headers,
                                        f"Content-Length: {len(body.encode())}", "", body])
                udp.sendto(response.encode(), address)

            try:
                while not stop.is_set():
                    try:
                        data, address = udp.recvfrom(65535)
                    except socket.timeout:
                        continue
                    message = data.decode()
                    method = message.split(" ", 1)[0]
                    if method == "SIP/2.0":
                        continue
                    events[port].append(method)
                    if method == "INVITE":
                        invite = message
                        reply(message, 180)
                        # Give both peers time to receive the parallel INVITEs.
                        time.sleep(0.15)
                        if mode == "decline":
                            reply(message, 603)
                        elif mode == "unavailable" and port == 5090:
                            reply(message, 404)
                        elif port == (5090 if mode == "browser_answers" else 5091):
                            sdp = ("v=0\r\no=fixture 1 1 IN IP4 127.0.0.1\r\ns=fixture\r\n"
                                   "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 6000 RTP/AVP 0\r\n"
                                   "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n")
                            reply(message, 200, sdp)
                    elif method == "CANCEL":
                        reply(message, 200)
                        if invite:
                            reply(invite, 487)
                    elif method == "BYE":
                        reply(message, 200)
            except Exception as error:
                errors.append(error)

        for port in events:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(("127.0.0.1", port))
            udp.settimeout(0.1)
            sockets.append(udp)
            thread = threading.Thread(target=serve, args=(udp, port), daemon=True)
            thread.start()
            threads.append(thread)
        logfile = root / "asterisk.log"
        ami = None
        with logfile.open("w") as log:
            proc = subprocess.Popen(["asterisk", "-f", "-g", "-vvv", "-C",
                                     str(root / "asterisk.conf")], stdout=log, stderr=log)
            try:
                for _ in range(60):
                    time.sleep(0.1)
                    try:
                        ami = socket.create_connection(("127.0.0.1", 5038), timeout=0.5)
                        break
                    except OSError:
                        if proc.poll() is not None:
                            break
                assert ami, logfile.read_text()[-3000:]
                ami.sendall(b"Action: Login\r\nUsername: fixture\r\nSecret: fixture-only\r\nEvents: off\r\n\r\n")
                time.sleep(0.2)
                ami.sendall(b"Action: Originate\r\nChannel: Local/s@fixture\r\nApplication: Wait\r\nData: 1\r\nAsync: true\r\n\r\n")
                for _ in range(80):
                    time.sleep(0.1)
                    if "RESULT=" in logfile.read_text():
                        break
            finally:
                if ami:
                    ami.close()
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                stop.set()
                for thread in threads:
                    thread.join(timeout=1)
                for udp in sockets:
                    udp.close()
        output = re.sub(r"\x1b\[[0-9;]*m", "", logfile.read_text())
        results = re.findall(r'"RESULT=([^"\n]*)', output)
        counts = [events[port].count("INVITE") for port in events]
        print({"mode": mode, "voicemail": voicemail, "original": original,
               "invites": counts, "result": results}, flush=True)
        assert not errors, errors
        assert results, output[-3000:]
        assert "UNEXPECTED_VOICEMAIL\"" not in output
        if original:
            assert sum(counts) == 1 and 0 in counts, counts
        else:
            assert counts == [1, 1], counts
            if mode != "decline":
                assert any(result.startswith("ANSWER:") for result in results), results
                if mode != "unavailable":
                    unanswered = 5091 if mode == "browser_answers" else 5090
                    assert "CANCEL" in events[unanswered], events


if __name__ == "__main__":
    assert os.environ.get("MDD_TEST_ISOLATED_ASTERISK") == "1", "Use an isolated --network none container"
    check("decline", original=True)
    for voicemail in (False, True):
        for mode in ("decline", "browser_answers", "mobile_answers", "unavailable"):
            check(mode, voicemail=voicemail)
    print("PASS: single-contact regression reproduced; eight parallel-ring scenarios passed")
