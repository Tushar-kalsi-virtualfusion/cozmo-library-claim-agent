"""Hardware policy must not bypass spoken consent or fabricate a fallback."""
import unittest

from claim_agent.onboarding import OnboardingSession


def confirmed(mode):
    return {"language": "English", "country": "India", "currency": "INR",
            "capture_mode": mode, "confirmed": True}


class CapturePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_lidar_capability_seeds_mode_without_confirming_or_starting(self):
        session = OnboardingSession("LiDAR phone", True, True)
        context = session.voice_context()
        self.assertEqual(context.get("capture_mode"), "lidar")
        self.assertTrue(context["lidar_supported"])
        self.assertIsNone(context["preferences"])
        self.assertFalse(context["capture_requested"])
        with self.assertRaises(ValueError):
            await session.invoke_voice_tool("request_capture", {"ready": True})
        await session.invoke_voice_tool("configure_session", confirmed("lidar"))
        self.assertFalse(session.capture_requested)
        await session.invoke_voice_tool("request_capture", {"ready": True})
        self.assertTrue(session.capture_requested)

    async def test_supported_phone_rejects_camera_confirmation_without_changing_setup(self):
        session = OnboardingSession("LiDAR phone", True, True)
        with self.assertRaisesRegex(ValueError, "capture mode"):
            await session.invoke_voice_tool("configure_session", confirmed("camera"))
        self.assertIsNone(session.preferences)
        await session.invoke_voice_tool("configure_session", confirmed("lidar"))
        with self.assertRaisesRegex(ValueError, "capture mode"):
            await session.invoke_voice_tool("configure_session", confirmed("camera"))
        self.assertEqual(session.preferences.capture_mode, "lidar")
        self.assertFalse(session.capture_requested)

    async def test_camera_fallback_only_when_lidar_is_unavailable(self):
        session = OnboardingSession("Camera phone", False, True)
        self.assertEqual(session.voice_context().get("capture_mode"), "camera")
        for mode in ("lidar", "comparison"):
            with self.assertRaisesRegex(ValueError, "capture mode"):
                await session.invoke_voice_tool("configure_session", confirmed(mode))
        await session.invoke_voice_tool("configure_session", confirmed("camera"))
        await session.invoke_voice_tool("request_capture", {"ready": True})
        self.assertTrue(session.capture_requested)

    async def test_explicit_comparison_retains_lidar_hardware_and_separate_readiness(self):
        session = OnboardingSession("LiDAR phone", True, True)
        await session.invoke_voice_tool("configure_session", confirmed("comparison"))
        self.assertEqual(session.preferences.capture_mode, "comparison")
        self.assertFalse(session.capture_requested)
        await session.invoke_voice_tool("request_capture", {"ready": True})
        self.assertTrue(session.capture_requested)

    async def test_no_camera_device_can_confirm_but_never_start_capture(self):
        session = OnboardingSession("Simulator", False, False)
        self.assertEqual(session.voice_context().get("capture_mode"), "camera")
        await session.invoke_voice_tool("configure_session", confirmed("camera"))
        with self.assertRaisesRegex(RuntimeError, "physical"):
            await session.invoke_voice_tool("request_capture", {"ready": True})
        self.assertFalse(session.capture_requested)
