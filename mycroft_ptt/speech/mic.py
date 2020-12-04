# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import audioop
from time import sleep
from collections import deque
import os
import pyaudio
import speech_recognition
from speech_recognition import (
    Microphone,
    AudioSource,
    AudioData
)
from threading import Lock

from mycroft_ptt.configuration import CONFIGURATION
from mycroft_ptt.speech.signal import check_for_signal
from mycroft_ptt.playback import play_audio, play_mp3, play_ogg, play_wav, \
    resolve_resource_file
from ovos_utils.log import LOG


class MutableStream:
    def __init__(self, wrapped_stream, format, muted=False):
        assert wrapped_stream is not None
        self.wrapped_stream = wrapped_stream

        self.muted = muted
        if muted:
            self.mute()

        self.SAMPLE_WIDTH = pyaudio.get_sample_size(format)
        self.muted_buffer = b''.join([b'\x00' * self.SAMPLE_WIDTH])
        self.read_lock = Lock()

    def mute(self):
        """Stop the stream and set the muted flag."""
        with self.read_lock:
            self.muted = True
            self.wrapped_stream.stop_stream()

    def unmute(self):
        """Start the stream and clear the muted flag."""
        with self.read_lock:
            self.muted = False
            self.wrapped_stream.start_stream()

    def read(self, size, of_exc=False):
        """Read data from stream.

        Arguments:
            size (int): Number of bytes to read
            of_exc (bool): flag determining if the audio producer thread
                           should throw IOError at overflows.

        Returns:
            (bytes) Data read from device
        """
        frames = deque()
        remaining = size
        with self.read_lock:
            while remaining > 0:
                # If muted during read return empty buffer. This ensures no
                # reads occur while the stream is stopped
                if self.muted:
                    return self.muted_buffer

                to_read = min(self.wrapped_stream.get_read_available(),
                              remaining)
                if to_read <= 0:
                    sleep(.01)
                    continue
                result = self.wrapped_stream.read(to_read,
                                                  exception_on_overflow=of_exc)
                frames.append(result)
                remaining -= to_read

        input_latency = self.wrapped_stream.get_input_latency()
        if input_latency > 0.2:
            LOG.warning("High input latency: %f" % input_latency)
        audio = b"".join(list(frames))
        return audio

    def close(self):
        self.wrapped_stream.close()
        self.wrapped_stream = None

    def is_stopped(self):
        try:
            return self.wrapped_stream.is_stopped()
        except Exception as e:
            LOG.error(repr(e))
            return True  # Assume the stream has been closed and thusly stopped

    def stop_stream(self):
        return self.wrapped_stream.stop_stream()


class MutableMicrophone(Microphone):
    def __init__(self, device_index=None, sample_rate=16000, chunk_size=1024,
                 mute=False):
        Microphone.__init__(self, device_index=device_index,
                            sample_rate=sample_rate, chunk_size=chunk_size)
        self.muted = False
        if mute:
            self.mute()

    def __enter__(self):
        return self._start()

    def _start(self):
        """Open the selected device and setup the stream."""
        assert self.stream is None, \
            "This audio source is already inside a context manager"
        self.audio = pyaudio.PyAudio()
        self.stream = MutableStream(self.audio.open(
            input_device_index=self.device_index, channels=1,
            format=self.format, rate=self.SAMPLE_RATE,
            frames_per_buffer=self.CHUNK,
            input=True,  # stream is an input stream
        ), self.format, self.muted)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._stop()

    def _stop(self):
        """Stop and close an open stream."""
        try:
            if not self.stream.is_stopped():
                self.stream.stop_stream()
            self.stream.close()
        except Exception:
            LOG.exception('Failed to stop mic input stream')
            # Let's pretend nothing is wrong...

        self.stream = None
        self.audio.terminate()

    def restart(self):
        """Shutdown input device and restart."""
        self._stop()
        self._start()

    def mute(self):
        self.muted = True
        if self.stream:
            self.stream.mute()

    def unmute(self):
        self.muted = False
        if self.stream:
            self.stream.unmute()

    def is_muted(self):
        return self.muted


def get_silence(num_bytes):
    return b'\0' * num_bytes


class ResponsiveRecognizer(speech_recognition.Recognizer):
    def __init__(self):

        self.config = CONFIGURATION
        listener_config = self.config.get('listener')

        self.overflow_exc = listener_config.get('overflow_exception', False)

        speech_recognition.Recognizer.__init__(self)
        self.audio = pyaudio.PyAudio()
        self.multiplier = listener_config.get('multiplier')
        self.energy_ratio = listener_config.get('energy_ratio')
        self.sec_between_signal_checks = \
            listener_config.get("sec_between_signal_checks", 0.2)
        self.recording_timeout_with_silence = \
            listener_config.get("recording_timeout_with_silence", 3.0)
        self.recording_timeout = listener_config.get("recording_timeout", 10.0)
        self.min_loud_sec = listener_config.get("min_loud_sec", 0.5)
        self.min_silence_at_end = \
            listener_config.get("min_silence_at_end", 0.25)
        self.ambient_noise_adjustment_time = listener_config.get(
            "ambient_noise_adjustment_time", 0.5)
        self.auto_ambient_noise_adjustment = listener_config.get(
            "auto_ambient_noise_adjustment", False)

        data_path = os.path.expanduser(self.config["data_dir"])
        if not os.path.isdir(data_path):
            os.makedirs(data_path)

        # Signal statuses
        self._stop_signaled = False
        self._listen_triggered = False
        self._should_adjust_noise = False

    def record_sound_chunk(self, source):
        return source.stream.read(source.CHUNK, self.overflow_exc)

    @staticmethod
    def calc_energy(sound_chunk, sample_width):
        return audioop.rms(sound_chunk, sample_width)

    def _record_phrase(
        self,
        source,
        sec_per_buffer,
        stream=None,
        ww_frames=None
    ):
        """Record an entire spoken phrase.

        Essentially, this code waits for a period of silence and then returns
        the audio.  If silence isn't detected, it will terminate and return
        a buffer of RECORDING_TIMEOUT duration.

        Args:
            source (AudioSource):  Source producing the audio chunks
            sec_per_buffer (float):  Fractional number of seconds in each chunk
            stream (AudioStreamHandler): Stream target that will receive chunks
                                         of the utterance audio while it is
                                         being recorded.
            ww_frames (deque):  Frames of audio data from the last part of wake
                                word detection.

        Returns:
            bytearray: complete audio buffer recorded, including any
                       silence at the end of the user's utterance
        """

        num_loud_chunks = 0
        noise = 0

        max_noise = 25
        min_noise = 0

        silence_duration = 0

        def increase_noise(level):
            if level < max_noise:
                return level + 200 * sec_per_buffer
            return level

        def decrease_noise(level):
            if level > min_noise:
                return level - 100 * sec_per_buffer
            return level

        # Smallest number of loud chunks required to return
        min_loud_chunks = int(self.min_loud_sec / sec_per_buffer)

        # Maximum number of chunks to record before timing out
        max_chunks = int(self.recording_timeout / sec_per_buffer)
        num_chunks = 0

        # Will return if exceeded this even if there's not enough loud chunks
        max_chunks_of_silence = int(self.recording_timeout_with_silence /
                                    sec_per_buffer)

        # bytearray to store audio in
        byte_data = get_silence(source.SAMPLE_WIDTH)

        if stream:
            stream.stream_start()

        phrase_complete = False
        while num_chunks < max_chunks and not phrase_complete:
            if ww_frames:
                chunk = ww_frames.popleft()
            else:
                chunk = self.record_sound_chunk(source)
            byte_data += chunk
            num_chunks += 1

            if stream:
                stream.stream_chunk(chunk)

            energy = self.calc_energy(chunk, source.SAMPLE_WIDTH)
            test_threshold = self.energy_threshold * self.multiplier
            is_loud = energy > test_threshold
            if is_loud:
                noise = increase_noise(noise)
                num_loud_chunks += 1
            else:
                noise = decrease_noise(noise)
                self._adjust_threshold(energy, sec_per_buffer)

            was_loud_enough = num_loud_chunks > min_loud_chunks

            quiet_enough = noise <= min_noise
            if quiet_enough:
                silence_duration += sec_per_buffer
                if silence_duration < self.min_silence_at_end:
                    quiet_enough = False  # gotta be silent for min of 1/4 sec
            else:
                silence_duration = 0
            recorded_too_much_silence = num_chunks > max_chunks_of_silence
            if quiet_enough and (was_loud_enough or recorded_too_much_silence):
                phrase_complete = True

            # Pressing top-button will end recording immediately
            if check_for_signal('buttonPress'):
                phrase_complete = True

        return byte_data

    @staticmethod
    def sec_to_bytes(sec, source):
        return int(sec * source.SAMPLE_RATE) * source.SAMPLE_WIDTH

    def _is_listen_signaled(self):
        """Check if told programatically to skip the wake word

        For example when we are in a dialog with the user.
        """
        if check_for_signal('startListening') or self._listen_triggered:
            return True

        # Pressing the button can start recording (unless
        # it is being used to mean 'stop' instead)
        if check_for_signal('buttonPress', 1):
            # give other processes time to consume this signal if
            # it was meant to be a 'stop'
            sleep(0.25)
            if check_for_signal('buttonPress'):
                # Signal is still here, assume it was intended to
                # begin recording
                LOG.debug("Button Pressed, listen signal not needed")
                return True

        return False

    def stop(self):
        """
            Signal stop and exit waiting state.
        """
        self._stop_signaled = True

    def trigger_listen(self):
        """Externally trigger listening."""
        LOG.debug('Listen triggered from external source.')
        self._listen_triggered = True

    def trigger_ambient_noise_adjustment(self):
        LOG.debug("Ambient noise adjustment requested from external source")
        self._should_adjust_noise = True

    def _adjust_ambient_noise(self, source, time=None):
        time = time or self.ambient_noise_adjustment_time
        LOG.info("Adjusting for ambient noise, be silent!!!")
        self.adjust_for_ambient_noise(source, time)
        LOG.info("Ambient noise profile has been created")
        self._should_adjust_noise = False

    def _wait_for_listen_signal(self, source):
        """Listen continuously on source until a listen signal is detected
        Args:
            source (AudioSource):  Source producing the audio chunks
            sec_per_buffer (float):  Fractional number of seconds in each chunk
        """

        while not self._stop_signaled and not self._is_listen_signaled():
            if check_for_signal('adjustAmbientNoise') or \
                    self._should_adjust_noise:
                self._adjust_ambient_noise(source)
            sleep(self.sec_between_signal_checks)

        # If enabled, play a wave file with a short sound to audibly
        # indicate listen signal was detected.
        sound = self.config["listener"].get('listen_sound')
        audio_file = resolve_resource_file(sound)
        try:
            if audio_file:
                if audio_file.endswith(".wav"):
                    play_wav(audio_file).wait()
                elif audio_file.endswith(".mp3"):
                    play_mp3(audio_file).wait()
                elif audio_file.endswith(".ogg"):
                    play_ogg(audio_file).wait()
                else:
                    play_audio(audio_file).wait()
        except Exception as e:
            LOG.warning(e)

    @staticmethod
    def _create_audio_data(raw_data, source):
        """
        Constructs an AudioData instance with the same parameters
        as the source and the specified frame_data
        """
        return AudioData(raw_data, source.SAMPLE_RATE, source.SAMPLE_WIDTH)

    def listen(self, source, bus, stream=None):
        """Listens for chunks of audio that Mycroft should perform STT on.

        This will listen continuously for a wake-up-word, then return the
        audio chunk containing the spoken phrase that comes immediately
        afterwards.

        Args:
            source (AudioSource):  Source producing the audio chunks
            bus (EventEmitter): Emitter for notifications of when recording
                                    begins and ends.
            stream (AudioStreamHandler): Stream target that will receive chunks
                                         of the utterance audio while it is
                                         being recorded

        Returns:
            AudioData: audio with the user's utterance, minus the wake-up-word
        """
        assert isinstance(source, AudioSource), "Source must be an AudioSource"

        #        bytes_per_sec = source.SAMPLE_RATE * source.SAMPLE_WIDTH
        sec_per_buffer = float(source.CHUNK) / source.SAMPLE_RATE

        LOG.debug("Muting microphone")
        LOG.debug("Waiting for listen signal...")
        source.mute()
        self._wait_for_listen_signal(source)
        self._listen_triggered = False
        if self._stop_signaled:
            return
        LOG.debug("Unmuting microphone for STT")
        source.unmute()
        LOG.debug("Recording...")
        bus.emit("recognizer_loop:record_begin")

        frame_data = self._record_phrase(source, sec_per_buffer, stream)
        audio_data = self._create_audio_data(frame_data, source)
        bus.emit("recognizer_loop:record_end")
        if self.auto_ambient_noise_adjustment:
            self._adjust_ambient_noise(source)
        LOG.debug("Thinking...")
        return audio_data

    def _adjust_threshold(self, energy, seconds_per_buffer):
        if self.dynamic_energy_threshold and energy > 0:
            # account for different chunk sizes and rates
            damping = (
                    self.dynamic_energy_adjustment_damping ** seconds_per_buffer)
            target_energy = energy * self.energy_ratio
            self.energy_threshold = (
                    self.energy_threshold * damping +
                    target_energy * (1 - damping))
