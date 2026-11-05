"""Voicemail: recording an unattended call, and keeping the recording out of everywhere it
does not belong.

The dialplan half is pinned by rendering the template, because a regression there is silent:
the call still completes, it just never records — and nobody notices until they need a message
that was never taken.
"""
import asyncio
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from control.app import store

try:
    from control.app import config as cfg, main, operations
except ImportError:                      # control-plane deps absent (fastapi et al.)
    cfg = main = operations = None

ROOT = Path(__file__).resolve().parent.parent
BASE_CTX = dict(webrtc_enable=True, webrtc_user="webrtc", ring_timeout=35, msisdn="+44",
                realm="ims", vm_ring_seconds=25, vm_max_seconds=120)


def render(**overrides) -> str:
    env = Environment(loader=FileSystemLoader(str(ROOT / "engine" / "templates")),
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    return env.get_template("extensions.conf.j2").render(**{**BASE_CTX, **overrides})


class DialplanTests(unittest.TestCase):
    def test_disabled_leaves_the_inbound_path_exactly_as_it_was(self):
        out = render(vm_enabled=False)
        self.assertIn("Set(BROWSER_PICKUP_DEADLINE=$[${EPOCH}+60])", out)
        self.assertIn("Set(BROWSER_CONTACTS=${PJSIP_DIAL_CONTACTS(webrtc)})", out)
        self.assertIn("same => n,Dial(${BROWSER_CONTACTS},${BROWSER_PICKUP_REMAINING})", out)
        self.assertIn("same => n(unanswered),NoOp(Browser pickup window expired)", out)
        self.assertNotIn("Record(", out)
        self.assertNotIn("[mdd_voicemail]", out)

    def test_enabled_replaces_the_ring_timeout_and_branches_to_the_recorder(self):
        out = render(vm_enabled=True)
        self.assertIn("Set(BROWSER_PICKUP_DEADLINE=$[${EPOCH}+25])", out)
        self.assertIn("same => n(unanswered),GotoIf", out)
        self.assertIn("same => n,Goto(mdd_voicemail,vm,1)", out)
        self.assertIn("Record(/logs/voicemail/", out)

    def test_an_offline_browser_is_waited_for_without_extending_the_answer_window(self):
        out = render(vm_enabled=True)
        wait = out.index("same => n(wait_browser),Set(BROWSER_CONTACTS=")
        dial = out.index("same => n(dial_browser),Set(BROWSER_PICKUP_REMAINING=")
        voicemail = out.index("same => n,Goto(mdd_voicemail,vm,1)")
        self.assertLess(wait, dial)
        self.assertLess(dial, voicemail)
        self.assertIn('GotoIf($["${DIALSTATUS}"="CHANUNAVAIL" & '
                      '${EPOCH}<${BROWSER_PICKUP_DEADLINE}]?wait_browser)', out)

    def test_a_declined_call_is_never_recorded(self):
        # BUSY on the inbound Dial means the user pressed decline. Recording then would be the
        # opposite of what they just asked for.
        out = render(vm_enabled=True)
        self.assertIn('same => n(unanswered),GotoIf($["${DIALSTATUS}"="BUSY"]?bye)', out)
        self.assertLess(out.index('GotoIf($["${DIALSTATUS}"="BUSY"]?bye)'),
                        out.index("Goto(mdd_voicemail,vm,1)"))

    def test_all_registered_clients_are_dialled_together(self):
        for vm_enabled in (False, True):
            with self.subTest(vm_enabled=vm_enabled):
                out = render(vm_enabled=vm_enabled, webrtc_user="mobile-clients")
                resolve = out.index('Set(BROWSER_CONTACTS=${PJSIP_DIAL_CONTACTS(mobile-clients)})')
                dial = out.index('same => n,Dial(${BROWSER_CONTACTS},${BROWSER_PICKUP_REMAINING})')
                self.assertLess(resolve, dial)
                self.assertNotIn('same => n,Dial(PJSIP/mobile-clients,', out)

    def test_sip_603_decline_stops_before_transport_retry_or_voicemail(self):
        for vm_enabled in (False, True):
            with self.subTest(vm_enabled=vm_enabled):
                out = render(vm_enabled=vm_enabled)
                dial = out.index('same => n,Dial(${BROWSER_CONTACTS},')
                declined = out.index('same => n,GotoIf($[${HANGUPCAUSE}=21]?bye)')
                retry = out.index('GotoIf($["${DIALSTATUS}"="CHANUNAVAIL"')
                self.assertLess(dial, declined)
                self.assertLess(declined, retry)
                self.assertIn('same => n(bye),Hangup()', out)

    def test_a_line_with_no_softphone_records_instead_of_answering_into_silence(self):
        out = render(vm_enabled=True, webrtc_enable=False)
        self.assertIn("same => n,Goto(mdd_voicemail,vm,1)", out)
        self.assertNotIn("same => n,Wait(30)", out)

    def test_the_recorder_lives_in_a_context_no_endpoint_can_reach(self):
        # [volte_ims] matches every printable extension, so a recorder sharing that context
        # could be reached by an INVITE whose user part happened to be "vm".
        out = render(vm_enabled=True)
        self.assertIn("[mdd_voicemail]", out)
        recorder = out.split("[mdd_voicemail]")[1]
        self.assertIn("exten => vm,1", recorder)
        volte = out.split("[volte_ims]")[1].split("\n[")[0]
        self.assertNotIn("exten => vm,1", volte)
        self.assertNotIn("context=mdd_voicemail",
                         (ROOT / "engine" / "templates" / "pjsip.conf.j2").read_text())

    def test_plain_h_handlers_end_with_hangup_not_return(self):
        # Both are ordinary special extensions, not Gosub routines. They still need an explicit
        # terminal application: otherwise [volte_ims]'s catch-all can match h at the next priority
        # and fire a phantom call_in, but Return() emits "Return without Gosub" on every call.
        out = render(vm_enabled=True)
        for context_name in ("volte_ims", "from-local"):
            context = out.split(f"[{context_name}]", 1)[1].split("\n[", 1)[0]
            handler = context.split("exten => h,1,", 1)[1].split("\n\n", 1)[0]
            self.assertNotIn("Return()", handler, context_name)
            self.assertTrue(handler.rstrip().endswith("same => n,Hangup()"), context_name)

    def test_gosub_routines_keep_their_returns(self):
        out = render(vm_enabled=True)
        outbound_headers = out.split("[ims-outbound-headers]", 1)[1].split("\n[", 1)[0]
        ussd_report = out.split("[ussd-report]", 1)[1].split("\n[", 1)[0]
        self.assertIn("same => n,Return()", outbound_headers)
        self.assertIn("same => n(done),Return()", ussd_report)
        hangup_by = out.split("[hangup-by]", 1)[1].split("\n[", 1)[0]
        self.assertIn("same => n,Return(${BY})", hangup_by)

    def test_both_h_handlers_report_which_side_ended_the_call(self):
        # ANSWER/16 after one second reads the same whether the carrier, the browser or the
        # gateway tore the call down (#109); the 'h' handlers name the side as a 5th argument.
        out = render(vm_enabled=True)
        for direction in ("in", "out"):
            line = next(l for l in out.splitlines() if f"call_result {direction} " in l)
            self.assertIn('"${GOSUB_RETVAL}" &)', line, direction)
            handler = out[:out.index(line)].rsplit("exten => h,1,", 1)[1]
            self.assertIn("Gosub(hangup-by,s,1)", handler, direction)
        routine = out.split("[hangup-by]", 1)[1].split("\n[", 1)[0]
        # The carrier's channel is named after its endpoint; a browser/MicroSIP leg is not.
        self.assertIn('"${HS:0:16}" = "PJSIP/volte_ims-"', routine)
        self.assertIn('?Set(BY=carrier)', routine)
        self.assertIn('?gateway:local', routine)
        self.assertTrue(routine.rstrip().endswith("same => n,Return(${BY})"))

    def test_the_prompt_is_shipped_rather_than_assumed(self):
        # The base image's Asterisk sound packages are a side effect of its build; nothing
        # here may depend on them existing.
        beep = ROOT / "engine" / "templates" / "sounds" / "vm-beep.wav"
        self.assertTrue(beep.is_file())
        with wave.open(str(beep)) as handle:
            self.assertEqual(handle.getframerate(), 8000)      # slin, no transcoding needed
            self.assertEqual(handle.getnchannels(), 1)
        self.assertIn("TryExec(Playback(", render(vm_enabled=True))


class VoicemailStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store_patch = patch.multiple(
            store, DATA_DIR=str(root), DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
            PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.store_patch.start()
        store.init()

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def test_recordings_are_bounded_by_count(self):
        # messages/calls grow forever and are trimmed by hand; audio cannot afford that on the
        # SD card an appliance boots from.
        for i in range(store.VOICEMAIL_KEEP_PER_LINE + 3):
            _, evicted = store.add_voicemail("1", "+1", f"voicemail/{i}.wav", 10, 1000,
                                             ts=1000 + i)
        self.assertEqual(len(store.list_voicemails("1", limit=999)),
                         store.VOICEMAIL_KEEP_PER_LINE)
        self.assertEqual(evicted, ["voicemail/2.wav"])

    def test_recordings_are_bounded_by_total_size(self):
        big = 100 * 1024 * 1024
        for i in range(4):
            _, evicted = store.add_voicemail("1", "+1", f"voicemail/{i}.wav", 60, big,
                                             ts=2000 + i)
        self.assertEqual(len(store.list_voicemails("1")), 2)
        self.assertTrue(evicted)

    def test_a_recording_finds_the_call_it_was_left_after(self):
        store.add_call("1", "in", "+447700900321", status="missed")
        record, _ = store.add_voicemail("1", "+447700900321", "voicemail/x.wav", 42, 5000)
        call = store.link_voicemail_to_call("1", "+447700900321", record["id"])
        self.assertEqual(call["voicemail_id"], record["id"])

    def test_unheard_counts_drive_the_unread_dot(self):
        store.add_voicemail("1", "+1", "voicemail/a.wav", 5, 100)
        record, _ = store.add_voicemail("2", "+1", "voicemail/b.wav", 5, 100)
        self.assertEqual(store.unheard_voicemail_counts(), {"1": 1, "2": 1})
        store.set_voicemail_listened("2", record["id"])
        self.assertEqual(store.unheard_voicemail_counts(), {"1": 1})

    def test_deleting_a_line_takes_its_recordings_with_it(self):
        store.add_voicemail("1", "+1", "voicemail/a.wav", 5, 100)
        store.clear_allowance_data("1")
        self.assertEqual(store.list_voicemails("1"), [])


@unittest.skipIf(main is None, "control-plane dependencies are unavailable")
class VoicemailIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store_patch = patch.multiple(
            store, DATA_DIR=str(self.root), DB_PATH=str(self.root / "mdd-sim-gateway.sqlite"),
            PREVIOUS_DB_PATH=str(self.root / "vowifi.sqlite"))
        self.store_patch.start()
        store.init()
        self.pushes = []
        inst = {"id": "1", "name": "UK SIM", "enabled": True, "sip": {"vm_enabled": True}}

        async def noop(*args, **kwargs):
            return None

        self.patches = [
            patch.object(cfg, "DATA_DIR", str(self.root)),
            patch.object(cfg, "get_instance", side_effect=lambda iid: inst),
            patch.object(cfg, "get_settings", return_value={"vm_enabled": False}),
            patch.object(main, "_dispatch_push",
                         side_effect=lambda ev, *a, **k: self.pushes.append(ev)),
            patch.object(main.hub, "broadcast", new=noop),
        ]
        for item in self.patches:
            item.start()
        # Module-level dedupe sets outlive one test; a stale id would silence a push another
        # test is asserting on.
        main.hub._pushed_calls.clear()
        main.hub._pushed_missed.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    def _write_wav(self, name, seconds=1):
        path = self.root / "instances" / "1" / "logs" / "voicemail" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x01" * (8000 * seconds))
        return path

    def _event(self, args):
        return asyncio.run(main.api_engine_event(
            {"instance": "1", "event": "voicemail_new", "args": args}))

    async def _result_then_settle(self):
        """Report the call as unanswered, then let the deferred missed-call push resolve."""
        await main.api_engine_event(
            {"instance": "1", "event": "call_result",
             "args": ["in", "+447700900321", "NOANSWER", "19"]})
        await asyncio.sleep(main.MISSED_CALL_VOICEMAIL_GRACE_SECONDS + 1)

    def test_a_recording_is_stored_attached_to_its_call_and_announced(self):
        asyncio.run(main.api_engine_event(
            {"instance": "1", "event": "call_in", "args": ["+447700900321"]}))
        asyncio.run(main.api_engine_event(
            {"instance": "1", "event": "call_result",
             "args": ["in", "+447700900321", "NOANSWER", "19"]}))
        self._write_wav("vm-1000-447700900321.wav", 2)
        result = self._event(["+447700900321",
                              "/logs/voicemail/vm-1000-447700900321.wav", "42"])
        self.assertTrue(result["ok"])
        record = store.list_voicemails("1")[0]
        self.assertEqual(record["duration_seconds"], 42)
        self.assertEqual(record["path"], "voicemail/vm-1000-447700900321.wav")
        call = [c for c in store.list_calls("1") if c["direction"] == "in"][0]
        self.assertEqual(call["voicemail_id"], record["id"])
        self.assertIn("voicemail_received", self.pushes)

    def test_a_path_outside_this_line_is_refused(self):
        # The engine token proves an event came from an engine container, not that its
        # arguments are sane; this path is one the manager will later open and serve.
        secret = self.root / "secret.wav"
        secret.write_bytes(b"not yours")
        for attempt in ("/logs/voicemail/../../../../secret.wav", "../../../secret.wav",
                        "/etc/passwd", "/logs/../../../secret.wav"):
            result = self._event(["+1", attempt, "5"])
            self.assertFalse(result["ok"], attempt)
        self.assertEqual(store.list_voicemails("1"), [])
        self.assertTrue(secret.exists())

    def test_a_message_replaces_the_missed_call_notification(self):
        # "You have a 0:42 message from X" already says everything "you missed a call from X"
        # says. Sending both makes one event buzz the user's phone twice.
        self._write_wav("vm-1000.wav", 2)
        asyncio.run(main.api_engine_event(
            {"instance": "1", "event": "call_in", "args": ["+447700900321"]}))
        self._event(["+447700900321", "/logs/voicemail/vm-1000.wav", "42"])
        asyncio.run(self._result_then_settle())
        self.assertIn("voicemail_received", self.pushes)
        self.assertNotIn("missed_call", self.pushes)

    def test_a_caller_who_leaves_no_message_still_reports_the_missed_call(self):
        asyncio.run(main.api_engine_event(
            {"instance": "1", "event": "call_in", "args": ["+447700900321"]}))
        asyncio.run(self._result_then_settle())
        self.assertIn("missed_call", self.pushes)
        self.assertNotIn("voicemail_received", self.pushes)

    def test_an_empty_recording_is_discarded_without_notifying(self):
        # Record()'s k option writes a header even when the caller hung up before speaking.
        empty = self._write_wav("vm-empty.wav")
        empty.write_bytes(b"")
        result = self._event(["+1", "/logs/voicemail/vm-empty.wav", "0"])
        self.assertEqual(result.get("dropped"), "empty_recording")
        self.assertEqual(self.pushes, [])
        self.assertFalse(empty.exists())


@unittest.skipIf(main is None, "control-plane dependencies are unavailable")
class VoicemailScopeTests(unittest.TestCase):
    """Per-line override vs system default. "Follow the default" has to stay distinguishable
    from "off for this line", or switching the global on would silently start recording on
    every line the user had merely left alone."""

    def test_a_line_can_override_the_system_default_in_both_directions(self):
        for line_value, glob, expected in [(True, False, True), (True, True, True),
                                           (False, True, False), (False, False, False),
                                           (None, True, True), (None, False, False)]:
            inst = {"sip": {} if line_value is None else {"vm_enabled": line_value}}
            with patch.object(cfg, "get_settings", return_value={"vm_enabled": glob}):
                self.assertEqual(main._voicemail_enabled(inst), expected,
                                 f"line={line_value} global={glob}")

    def test_choosing_the_default_removes_the_override_rather_than_storing_false(self):
        # A stored False would pin the line off forever, and the UI would show "off" where the
        # user chose "follow the default".
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(cfg, "DATA_DIR", str(root)), \
                 patch.object(cfg, "CONFIG_PATH", str(root / "config.yaml")):
                base = {"id": "1", "name": "line", "imsi": "001010000000001"}
                cfg.upsert_instance({**base, "sip": {"vm_enabled": True}})
                self.assertIs((cfg.get_instance("1")["sip"]).get("vm_enabled"), True)
                # The browser omits the key entirely for "use the default".
                cfg.upsert_instance({**base, "sip": {"listen_addr": "0.0.0.0"}})
                self.assertNotIn("vm_enabled", cfg.get_instance("1")["sip"])


@unittest.skipIf(operations is None, "control-plane dependencies are unavailable")
class VoicemailPrivacyTests(unittest.TestCase):
    def test_a_support_bundle_never_carries_a_recording(self):
        # A bundle is meant to be shareable. A recording of a caller's voice cannot be
        # redacted into something safe to share, so it must never be collected at all.
        import io
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "instances" / "1" / "logs"
            (logs / "voicemail").mkdir(parents=True)
            (logs / "voicemail" / "vm-1000-447700900321.wav").write_bytes(b"RIFFfake")
            (logs / "diagnostics.jsonl").write_text('{"note":"ordinary log"}\n')
            with patch.object(cfg, "DATA_DIR", str(root)), \
                 patch.object(cfg, "get_settings", return_value={}), \
                 patch.object(operations, "host_diagnostics", return_value={}):
                blob = operations.support_bundle({}, 100)
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
        self.assertTrue(any("diagnostics" in name for name in names), names)
        self.assertFalse([name for name in names if name.endswith(".wav")], names)
        self.assertFalse([name for name in names if "voicemail" in name], names)

    def test_the_engine_event_log_filter_drops_voicemail_lines(self):
        # events.jsonl carries the caller's number in an args array the key-name redaction
        # rules cannot reach, so the bundle keeps only an explicit allow-list of events.
        self.assertNotIn("voicemail_new", operations.CALL_EVENTS)
        kept = operations.call_event_evidence(
            '{"event":"voicemail_new","args":["+447700900321","/logs/voicemail/x.wav","42"]}\n'
            '{"event":"call_result","args":["in","#225#","ANSWER","16"]}')
        self.assertNotIn("447700900321", kept)
        self.assertNotIn("voicemail", kept)


if __name__ == "__main__":
    unittest.main()
