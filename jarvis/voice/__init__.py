"""Voice subsystem.

Import-safe with *none* of the optional voice stack installed: every heavy
dependency (faster-whisper, sounddevice, soundfile, openwakeword, piper,
numpy) is imported lazily inside the functions that need it. Capabilities report
themselves available or unavailable-with-a-reason through
``VoiceService.status()``; nothing pretends to work.
"""

from .pipeline import VoiceService, VoiceStatus

__all__ = ["VoiceService", "VoiceStatus"]
