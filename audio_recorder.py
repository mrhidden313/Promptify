"""
audio_recorder.py
Handles microphone audio capture in-memory using sounddevice and numpy.
Compiles recorded audio into 16kHz mono WAV bytes ready for Gemini API.
Zero disk I/O ensures high performance and prevents temporary file clutter.
"""

import io
import wave
import threading
import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None


class AudioRecorderError(Exception):
    """Raised when audio capture initialization or recording fails."""
    pass


class AudioRecorder:
    """Thread-safe in-memory microphone audio recorder."""

    SAMPLE_RATE = 16000  # 16kHz is optimal for speech recognition
    CHANNELS = 1         # Mono

    def __init__(self):
        self._recording = False
        self._chunks = []
        self._lock = threading.Lock()
        self._stream = None

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for each new audio block from the microphone."""
        if status:
            pass
        with self._lock:
            if self._recording:
                # Copy numpy buffer to keep it safe from stream overwrite
                self._chunks.append(indata.copy())

    def start_recording(self):
        """Start listening to the default microphone."""
        if sd is None:
            raise AudioRecorderError("sounddevice package is missing. Run pip install sounddevice.")

        with self._lock:
            if self._recording:
                return
            self._chunks = []
            self._recording = True

        try:
            self._stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            with self._lock:
                self._recording = False
                self._chunks = []
            if self._stream:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            raise AudioRecorderError(f"Could not access microphone: {exc}") from exc

    def stop_recording(self) -> bytes:
        """
        Stop recording, finalize the audio stream, and return in-memory WAV bytes.
        Returns empty bytes if no audio was captured.
        """
        with self._lock:
            if not self._recording:
                return b""
            self._recording = False

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None

        with self._lock:
            chunks = list(self._chunks)
            self._chunks = []

        if not chunks:
            return b""

        # Concatenate numpy arrays into single contiguous buffer
        audio_data = np.concatenate(chunks, axis=0)

        # Build in-memory WAV file
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())

        return buffer.getvalue()

