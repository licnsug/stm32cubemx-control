#!/usr/bin/env python3
"""Offline tests for the pure logic of the CubeMX toolkit.

Nothing here starts CubeMX or touches a real project, so it runs in a second and
covers the bookkeeping that is easy to get wrong: key escaping, IP renumbering,
the backup/restore round trip and the console verdict parser.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cubemx  # noqa: E402


class EscapingTest(unittest.TestCase):
    def test_key_escaping(self):
        self.assertEqual(cubemx.escape_ioc_key("TIM1.Channel-PWM Generation1 CH1 CH1N"),
                         "TIM1.Channel-PWM\\ Generation1\\ CH1\\ CH1N")
        self.assertEqual(cubemx.escape_ioc_key("NVIC.SysTick_IRQn"),
                         "NVIC.SysTick_IRQn")

    def test_expand_mcu_ref(self):
        self.assertIn("stm32g431cbux", cubemx.expand_mcu_ref("STM32G431C(6-8-B)Ux"))
        self.assertIn("stm32g431c6ux", cubemx.expand_mcu_ref("STM32G431C(6-8-B)Ux"))


class IocTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "t.ioc"
        self.path.write_text(
            "Mcu.IP0=NVIC\nMcu.IP1=RCC\nMcu.IP2=SYS\nMcu.IPNb=3\n"
            "Mcu.Pin0=PA13\nMcu.PinsNb=1\nVP_SYS_VS_Systick.Mode=SysTick\n",
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_set_and_get_escaped_key(self):
        ioc = cubemx.Ioc(self.path)
        ioc.set("TIM1.Channel-PWM Generation1 CH1 CH1N", "TIM_CHANNEL_1")
        ioc.save()
        again = cubemx.Ioc(self.path)
        self.assertEqual(again.get("TIM1.Channel-PWM Generation1 CH1 CH1N"),
                         "TIM_CHANNEL_1")

    def test_add_and_remove_ip_renumber(self):
        ioc = cubemx.Ioc(self.path)
        ioc.add_ip("TIM1", "ControllerModeReset")
        self.assertEqual(ioc.ip_list(), ["NVIC", "RCC", "SYS", "TIM1"])
        self.assertEqual(ioc.get("Mcu.IPNb"), "4")
        self.assertEqual(ioc.get("VP_TIM1_VS_ControllerModeReset.Mode"), "ControllerModeReset")
        ioc.remove_ip("TIM1")
        self.assertEqual(ioc.ip_list(), ["NVIC", "RCC", "SYS"])
        self.assertEqual(ioc.get("Mcu.IPNb"), "3")
        self.assertIsNone(ioc.get("VP_TIM1_VS_ControllerModeReset.Mode"))


class MergeTest(unittest.TestCase):
    def test_function_list_union_renumbers(self):
        merged = cubemx.merge_function_lists(
            "1-SystemClock_Config-RCC-false-HAL-false,2-MX_GPIO_Init-GPIO-false-HAL-true",
            "1-MX_GPIO_Init-GPIO-false-HAL-true,2-SystemClock_Config-RCC-false-HAL-false,"
            "3-MX_TIM1_Init-TIM1-false-HAL-true")
        self.assertIn("MX_TIM1_Init", merged)
        self.assertTrue(merged.startswith("1-SystemClock_Config"))
        self.assertEqual(merged.count("MX_GPIO_Init"), 1)


class DiffTest(unittest.TestCase):
    def test_drop_add_change(self):
        diff = cubemx.diff_ioc_keys("A=1\nB=2\n", "A=9\nC=3\n")
        self.assertEqual(diff["dropped"], ["B"])
        self.assertEqual(diff["added"], ["C"])
        self.assertEqual(diff["changed"], ["A"])


class VerdictTest(unittest.TestCase):
    LOG = (
        "2026-09-22 22:35:09,050 [INFO] CodeEngine:319 - Generated code\n"
        "config load E:/p/p.ioc\n"
        "OK\n"
        "set mode TIM1 \"PWM Generation1 CH1 CH1N\"\n"
        "OK\n"
        "set mode BAD nope\n"
        "KO\n"
        "project generate\n"
        "java.lang.NullPointerException: boom\n"
        "\tat com.st.Thing.run(Thing.java:1)\n"
        "KO\n"
        "exit\n"
        "Bye bye\n")

    def test_pairs_commands_with_verdicts(self):
        results = cubemx._command_results(self.LOG)
        self.assertEqual([r["result"] for r in results], ["OK", "OK", "KO", "KO"])
        self.assertEqual(results[1]["command"], 'set mode TIM1 "PWM Generation1 CH1 CH1N"')

    def test_run_succeeded_needs_no_ko(self):
        self.assertFalse(cubemx._run_succeeded(
            {"bye_bye": True, "timed_out": False, "stalled": False,
             "failed_commands": ["project generate"]}))
        self.assertFalse(cubemx._run_succeeded(
            {"bye_bye": True, "timed_out": False, "stalled": True, "failed_commands": []}))
        self.assertTrue(cubemx._run_succeeded(
            {"bye_bye": True, "timed_out": False, "stalled": False, "failed_commands": []}))


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "work"
        self.proj = self.tmp / "proj"
        (self.proj / "MDK-ARM" / "out").mkdir(parents=True)
        (self.proj / "Core").mkdir(parents=True)
        (self.proj / "p.ioc").write_text("Mcu.IPNb=3\n", encoding="utf-8")
        (self.proj / "Core" / "main.c").write_text("int main(void){return 0;}\n",
                                                   encoding="utf-8")
        for name in ("a.o", "b.crf", "c.axf"):
            (self.proj / "MDK-ARM" / "out" / name).write_bytes(b"x" * 1000)

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_artifacts_skipped_and_restore_round_trip(self):
        info = cubemx.backup_project(self.proj, self.work, "proj")
        self.assertEqual(info["files"], 2)          # the .ioc and main.c only
        self.assertEqual(info["skipped_artifacts"], 3)
        expected = sum(p.stat().st_size for p in
                       (self.proj / "p.ioc", self.proj / "Core" / "main.c"))
        self.assertEqual(info["bytes"], expected)

        (self.proj / "p.ioc").write_text("BROKEN\n", encoding="utf-8")
        result = cubemx.restore_backup(self.work, self.proj, Path(info["path"]).name)
        self.assertTrue(result["ok"])
        self.assertEqual((self.proj / "p.ioc").read_text(encoding="utf-8"), "Mcu.IPNb=3\n")

    def test_rotation_keeps_newest(self):
        for _ in range(3):
            cubemx.backup_project(self.proj, self.work, "proj", keep=2)
        self.assertEqual(len(cubemx.list_backups(self.work, "proj")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
