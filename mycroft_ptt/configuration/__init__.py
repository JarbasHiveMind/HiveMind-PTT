from json_database import JsonStorageXDG
from os.path import exists, expanduser, join
from tempfile import gettempdir

DEFAULT_CONFIGURATION = {
    'data_dir': expanduser('~/jarbasHiveMind/recordings'),
    'host': '0.0.0.0',
    'lang': 'en-us',
    'listener': {
        # should recordings be saved? {data_dir}/recordings/utterances
        'record_utterances': False,
        # input stream config
        'channels': 1,
        'sample_rate': 16000,
        # noise detection
        'energy_ratio': 1.5,
        'multiplier': 1.0,
        # The minimum seconds of noise before a
        # phrase can be considered complete
        "min_loud_sec": 0.7,
        # The minimum seconds of silence required at the end
        # before a phrase will be considered complete
        "min_silence_at_end": 0.3,
        # The maximum seconds a phrase can be recorded,
        # provided there is noise the entire time
        "recording_timeout": 10,
        # The maximum time it will continue to record silence
        # when not enough noise has been detected
        "recording_timeout_with_silence": 3,
        # Time between checks for listen signal
        "sec_between_signal_checks": 0.2,
        # Auto adjust for ambient noise (after recording, before STT
        # processing, introduces latency for time defined bellow)
        "auto_ambient_noise_adjustment": True,
        # Time to listen and adjust for ambient noise in seconds
        "ambient_noise_adjustment_time": 0.5,
        # checks for {signal_folder}/signal/startListening
        'signal_folder': join(gettempdir(), "hivemind", "ipc"),
        # can be set to None or full file path
        'listen_sound': 'snd/start_listening.wav',
        'error_sound': 'snd/listening_error.mp3'
    },

    'playback': {
        'play_wav_cmd': "aplay %1",
        'play_mp3_cmd': "mpg123 %1",
        'play_ogg_cmd': "ogg123 -q %1",
        'play_fallback_cmd': "play %1"
    },

    'log_blacklist': [],
    'port': 5678,
    'stt': {'deepspeech_server': {'uri': 'http://localhost:8080/stt'},
            'deepspeech_stream_server': {
                'stream_uri': 'http://localhost:8080/stt?format=16K_PCM16'},
            'kaldi': {
                'uri': 'http://localhost:8080/client/dynamic/recognize'},
            'kaldi_vosk': {'model': '/path/to/model/folder'},
            'kaldi_vosk_streaming': {'model': '/path/to/model/folder'},
            "deepspeech": {"model": "path/to/model.pbmm",
                           "scorer": "path/to/model.scorer"},
            "deepspeech_streaming": {"model": "path/to/model.pbmm",
                                     "scorer": "path/to/model.scorer"},
            'module': 'google'},
    'tts': {'module': 'responsive_voice'}}


def _merge_defaults(base, default=None):
    """
        Recursively merging configuration dictionaries.

        Args:
            base:  Target for merge
            default: Dictionary to merge into base if key not present
    """
    default = default or DEFAULT_CONFIGURATION
    for k, dv in default.items():
        bv = base.get(k)
        if isinstance(dv, dict) and isinstance(bv, dict):
            _merge_defaults(bv, dv)
        elif k not in base:
            base[k] = dv
    return base


CONFIGURATION = JsonStorageXDG("HivemindPtT")
CONFIGURATION = _merge_defaults(CONFIGURATION)
if not exists(CONFIGURATION.path):
    CONFIGURATION.store()
