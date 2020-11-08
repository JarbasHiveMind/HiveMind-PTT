from mycroft_ptt.speech.listener import RecognizerLoop
from mycroft_ptt.configuration import CONFIGURATION
from jarbas_hive_mind.slave.terminal import HiveMindTerminalProtocol, \
    HiveMindTerminal
from jarbas_hive_mind import HiveMindConnection
from jarbas_utils.log import LOG
from jarbas_utils import create_daemon
from jarbas_utils.messagebus import Message
from text2speech import TTSFactory
from tempfile import gettempdir
from os.path import join, isdir
from os import makedirs
from jarbas_utils.sound import play_audio


class JarbasPtTTerminalProtocol(HiveMindTerminalProtocol):

    def onOpen(self):
        super().onOpen()
        create_daemon(self.factory.start_listening)

    def onClose(self, wasClean, code, reason):
        super().onClose(wasClean, code, reason)
        if "WebSocket connection upgrade failed" in reason:
            utterance = "hive mind refused connection, invalid password"
            self.factory.speak(utterance)
        else:
            self.factory.stop_listening()


class JarbasPtTTerminal(HiveMindTerminal):
    protocol = JarbasPtTTerminalProtocol
    platform = "JarbasPushToTalkTerminalV0.1"

    def __init__(self, config=CONFIGURATION, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config
        self.loop = RecognizerLoop(self.config)
        self.tts = TTSFactory.create(self.config["tts"])
        LOG.debug("Using TTS engine: " + self.tts.__class__.__name__)
        self.tts.validate()

    # Voice Output
    def speak(self, utterance):
        LOG.info("SPEAK: " + utterance)
        temppath = join(gettempdir(), self.tts.tts_name)
        if not isdir(temppath):
            makedirs(temppath)
        file_path = join(temppath, str(hash(utterance))[1:] +
                         "." + self.tts.audio_ext)
        self.tts.get_tts(utterance, file_path)
        play_audio(file_path).wait()

    # Voice Input
    def handle_record_begin(self):
        LOG.info("Begin Recording...")

    def handle_record_end(self):
        LOG.info("End Recording...")

    def handle_utterance(self, event):
        context = {'platform': self.platform, "source": self.peer,
                   'destination': "hive_mind"}
        msg = {"data": {"utterances": event['utterances'], "lang": "en-us"},
               "type": "recognizer_loop:utterance",
               "context": context}

        self.send_to_hivemind_bus(msg)

    def handle_unknown(self):
        LOG.info("mycroft.speech.recognition.unknown")

    def handle_sleep(self):
        self.loop.sleep()

    def handle_mic_mute(self, event):
        self.loop.mute()

    def handle_mic_unmute(self, event):
        self.loop.unmute()

    def handle_audio_start(self, event):
        """
            Mute recognizer loop
        """
        self.loop.mute()

    def handle_audio_end(self, event):
        """
            Request unmute, if more sources has requested the mic to be muted
            it will remain muted.
        """
        self.loop.unmute()  # restore

    def handle_stop(self, event):
        """
            Handler for mycroft.stop, i.e. button press
        """
        self.loop.force_unmute()

    def start_listening(self):
        self.loop.on('recognizer_loop:utterance',
                     self.handle_utterance)
        self.loop.on('recognizer_loop:record_begin',
                     self.handle_record_begin)
        self.loop.on('recognizer_loop:record_end',
                     self.handle_record_end)
        self.loop.run()

    def stop_listening(self):
        self.loop.remove_listener('recognizer_loop:utterance',
                                  self.handle_utterance)
        self.loop.remove_listener('recognizer_loop:record_begin',
                                  self.handle_record_begin)
        self.loop.remove_listener('recognizer_loop:record_end',
                                  self.handle_record_end)

    # parsed protocol messages
    def handle_incoming_mycroft(self, message):
        assert isinstance(message, Message)
        if message.msg_type == "speak":
            utterance = message.data["utterance"]
            self.speak(utterance)
        elif message.msg_type == "hive.complete_intent_failure":
            LOG.error("complete intent failure")
            self.speak('I don\'t know how to answer that')


def connect_to_hivemind(config=CONFIGURATION, host="wss://127.0.0.1",
                        port=5678, name="JarbasPTTTerminal",
                        access_key="RESISTENCEisFUTILE",
                        crypto_key="resistanceISfutile"):
    con = HiveMindConnection(host, port)

    terminal = JarbasPtTTerminal(config=config,
                                 crypto_key=crypto_key,
                                 headers=con.get_headers(name, access_key))

    con.connect(terminal)
