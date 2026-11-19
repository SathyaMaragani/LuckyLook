"""LuckyLook's voice, all on this PC: the microphone, the "Hey Lucky" wake word (Vosk), speech to text (Whisper,
through faster-whisper) and text to speech (Piper). The models live in voice-models/ (install.bat downloads them).
Nothing you say leaves the computer."""
import collections
import json
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
MODELS = HERE / "voice-models"
WAKE_WORD = True  # listen for "Hey Lucky" all the time (the mic stays open; the tray menu turns it off)
WHISPER_MODEL = "small"  # tiny, base, small, medium: bigger understands better, takes longer
VOICE = "af_heart"  # a Kokoro voice (natural): af_heart, af_bella, af_sarah (American), bf_emma, bf_isabella
# (British), hf_alpha (Hindi)... or a Piper voice like en_US-lessac-medium (smaller, more robotic)
SPEED = 1.0  # how fast it talks
PIPER_VOICE = "en_US-lessac-medium"  # used when Kokoro's model isn't downloaded
KOKORO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_LANG = {"a": "en-us", "b": "en-gb", "h": "hi", "e": "es", "f": "fr-fr", "i": "it", "j": "ja", "p": "pt-br",
               "z": "cmn"}  # by a Kokoro voice's first letter
SILENCE_SECONDS = 0.8  # this long quiet after speaking ends a request
MAX_SECONDS = 20
GIVE_UP_SECONDS = 6  # nothing said for this long: stop listening
UNLOAD_MINUTES = 10  # Whisper (about 500 MB) is unloaded after this long without voice use
RATE = 16000  # what Whisper and Vosk want
BLOCK = 1600  # 0.1 s
WAKE_HINT = r"\b(hey|hi|okay|ok|a) (lucky|lucy|look|luck|locke|lockie|lucky's)\b"  # what Vosk may make of it
WAKE_SAID = r"\b(hey|hi|okay|ok)\W+lucky\b"  # what Whisper must hear for it to count
YES = r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|allow|please do|haan|ha)\b"
NO = r"\b(no|nope|don'?t|stop|cancel|deny|never ?mind|nahi|na)\b"

try:
    from local_settings import *  # noqa: F401,F403  your own settings, never uploaded
except ImportError:
    pass


def download():
    """Downloads the models that aren't there yet (install.bat runs this): about 950 MB the first time."""
    import io
    import subprocess
    import sys
    import urllib.request
    import zipfile
    MODELS.mkdir(exist_ok=True)
    if not (MODELS / f"whisper-{WHISPER_MODEL}" / "model.bin").exists():
        print(f"Whisper {WHISPER_MODEL} (speech to text)...")
        from faster_whisper import download_model
        download_model(WHISPER_MODEL, output_dir=str(MODELS / f"whisper-{WHISPER_MODEL}"))
    for f in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        if not (MODELS / "kokoro" / f).exists():
            print(f"Kokoro {f} (the natural voice)...")
            (MODELS / "kokoro").mkdir(exist_ok=True)
            urllib.request.urlretrieve(KOKORO + f, MODELS / "kokoro" / (f + ".part"))
            (MODELS / "kokoro" / (f + ".part")).rename(MODELS / "kokoro" / f)  # only whole files count
    if not (MODELS / "piper" / f"{PIPER_VOICE}.onnx").exists():
        print(f"Piper voice {PIPER_VOICE} (the small fallback voice)...")
        subprocess.run([sys.executable, "-m", "piper.download_voices", PIPER_VOICE, "--download-dir",
                        str(MODELS / "piper")], check=True)
    if not list(MODELS.glob("vosk-model-small-en-us*")):
        print("Vosk small English (the wake word)...")
        with urllib.request.urlopen("https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip", timeout=300) as r:
            zipfile.ZipFile(io.BytesIO(r.read())).extractall(MODELS)
    print("voice models ready")


# ---------- what gets said ----------

def spoken(text):
    """An answer's markdown as something to read aloud: no symbols, code and tables pointed at, paths as file names."""
    text = re.sub(r"```.*?(```|$)", " I've put the code on screen. ", text, flags=re.S)
    text = re.sub(r"(?m)(^\s*\|.*\|\s*$\n?)+", " The details are in the table on screen. ", text)
    text = re.sub(r"`([A-Za-z]:\\[^`]*?)\\?`", lambda m: Path(m[1]).name or m[1], text)  # `D:\x\cv.pdf` -> cv.pdf
    text = re.sub(r"\b[A-Za-z]:\\[^\s`\"'<>|]*", lambda m: Path(m[0].rstrip(".,;:")).name or m[0], text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[`*_#>|]|^\s*[-•]\s+|^\s*\d+[.)]\s+", " ", text, flags=re.M)
    text = re.sub(r"[^\w\s.,;:!?'\"()%°/&+-]", " ", text)  # emoji and other symbols Piper would spell out
    return re.sub(r"\s+", " ", text).strip()


def people():
    """The names under a "People" heading in me.md ("- Yeshwanth (college friend)" -> Yeshwanth): Whisper is told to
    expect them, so it spells them right."""
    try:
        text = (HERE / "me.md").read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"(?ims)^#+\s*people\b[^\n]*\n(.*?)(?=^#|\Z)", text)
    return [n.strip() for n in re.findall(r"(?m)^\s*[-*]\s*([^:(\-–—\n]+)", m[1])] if m else []


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def strip_wake(text):
    """What was asked, without the wake phrase Whisper heard in front of it."""
    return re.sub(r"^(\W*((hey|hi|okay|ok)\W+)?lucky\b\W*)+", "", text, flags=re.I).strip()


def answer_yes(text):
    """True for yes, False for no, None if it's neither."""
    t = text.lower()
    return False if re.search(NO, t) else True if re.search(YES, t) else None


# ---------- listening ----------

def until_silence(blocks, give_up=GIVE_UP_SECONDS):
    """Collects int16 blocks of speech until SILENCE_SECONDS of quiet after it (or MAX_SECONDS, or nothing said for
    give_up seconds: then None). The threshold follows the room's own noise level."""
    out, floor, heard, quiet = [], None, False, 0.0
    step = BLOCK / RATE
    for i, block in enumerate(blocks):
        out.append(block)
        rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
        floor = rms if floor is None else min(floor * 1.02 + 1, rms) if not heard else floor
        loud = rms > max(3 * floor, 400)
        heard |= loud
        quiet = 0.0 if loud else quiet + step
        if heard and quiet >= SILENCE_SECONDS or (i + 1) * step >= MAX_SECONDS:
            break
        if not heard and (i + 1) * step >= give_up:
            return None
    return np.concatenate(out) if heard else None


class Voice:
    """One microphone stream shared by the wake word and requests; Whisper and Piper loaded when first needed.
    Callbacks: on_wake() when the wake phrase is heard, on_level(0..1) while recording."""

    def __init__(self, on_wake=lambda: None, on_heard=lambda text: None, on_level=lambda x: None, paused=lambda: False):
        self.on_wake, self.on_heard, self.on_level, self.paused = on_wake, on_heard, on_level, paused
        self.blocks = queue.Queue()
        self.stream = None
        self.wake_on = False
        self.recording = None  # a queue the recorder reads from, while one runs
        self.ring = collections.deque(maxlen=30)  # the last 3 s: "Hey Lucky, what's..." is all kept
        self.speaking = threading.Event()
        self.stop_speaking = threading.Event()
        self.lock = threading.Lock()
        self.whisper = self.piper = self.vosk = self.tts = None
        self.spoke = 0.0
        self.used = time.time()
        threading.Thread(target=self.run, daemon=True).start()
        threading.Thread(target=self.unload_loop, daemon=True).start()

    # --- the microphone ---

    def open(self):
        with self.lock:
            if not self.stream:
                import sounddevice as sd
                self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                             callback=lambda data, frames, t, status: self.blocks.put(data[:, 0].copy()))
                self.stream.start()

    def close_if_idle(self):
        with self.lock:
            if self.stream and not self.wake_on and self.recording is None:
                self.stream.stop()
                self.stream.close()
                self.stream = None  # the mic is off (and Windows' mic icon goes away)

    def wake(self, on):
        """Turns listening for the wake phrase on or off."""
        self.wake_on = bool(on)
        if on:
            self.open()
        else:
            self.close_if_idle()

    def run(self):
        """Hands each 0.1 s of sound to whoever needs it: a recording, or else the wake word. The wake word is two
        steps: Vosk (cheap, only while there's sound) hears something like "hey lucky", then Whisper checks those
        last seconds. Recording starts at once, so "Hey Lucky, what's the time" in one breath loses nothing."""
        rec, floor = None, 300.0
        while True:
            block = self.blocks.get()
            if self.recording is not None:
                self.recording.put(block)
                continue
            self.ring.append(block)
            if not self.wake_on or self.speaking.is_set() or self.paused():
                rec = None  # start fresh afterwards
                continue
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
            floor = min(floor * 1.01 + 1, rms)
            if rec is None:
                if rms < max(3 * floor, 400):
                    continue  # quiet: no speech recognition at all
                rec = self.wake_recognizer()
            ended = rec.AcceptWaveform(block.tobytes())  # a short "Hey Lucky." can end the utterance in one go
            heard = json.loads(rec.Result()).get("text", "") if ended else json.loads(rec.PartialResult()).get("partial", "")
            if ended:
                rec = None  # idle until the next sound
            if re.search(WAKE_HINT, heard):
                rec = None
                q, pre = queue.Queue(), list(self.ring)
                self.ring.clear()
                for b in pre:
                    q.put(b)
                self.recording = q
                threading.Thread(target=self.woken, args=(q, pre), daemon=True).start()

    def woken(self, q, pre):
        """Was it really the wake phrase? If so: on_wake(), then the request (which may already be underway)."""
        if not re.search(WAKE_SAID, self.transcribe(np.concatenate(pre)), re.I):
            if self.recording is q:
                self.recording = None  # a false alarm: back to listening, nothing shown
            return
        self.on_wake()
        text = strip_wake(self.listen(q, release=False))  # the mic stays with this request meanwhile
        if not text:  # "Hey Lucky." ... pause ... the request, which kept coming in while that was transcribed
            text = strip_wake(self.listen(q))
        else:
            self.listen_done(q)
        self.on_heard(text)

    def wake_recognizer(self):
        from vosk import KaldiRecognizer, Model, SetLogLevel
        if not self.vosk:
            SetLogLevel(-1)
            self.vosk = Model(str(next(MODELS.glob("vosk-model-small-en-us*"))))
        return KaldiRecognizer(self.vosk, RATE)

    def record(self, give_up=GIVE_UP_SECONDS):
        """Records one request (until a pause) and returns the text, or "" if nothing was said."""
        self.shut_up()
        self.open()
        q = queue.Queue()
        self.recording = q
        return self.listen(q, give_up)

    def listen(self, q, give_up=GIVE_UP_SECONDS, release=True):
        """Text of the next request in q's sound. release=False keeps the mic on q afterwards (then listen_done)."""
        def blocks():
            while self.recording is q:
                try:
                    b = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                self.on_level(min(1.0, float(np.abs(b).mean()) / 3000))
                yield b
        cancelled = True
        try:
            audio = until_silence(blocks(), give_up)
            cancelled = self.recording is not q
        finally:
            if release or cancelled:
                self.listen_done(q)
        if audio is None or cancelled:
            return ""
        self.on_level(-1)  # recorded: now understanding it
        return self.transcribe(audio)

    def listen_done(self, q):
        if self.recording is q:
            self.recording = None
        self.on_level(0)
        self.close_if_idle()

    def cancel(self):
        """Stops a recording now (what was said so far is dropped)."""
        if self.recording is not None:
            self.recording = None

    def transcribe(self, audio):
        """int16 or float audio at RATE -> text."""
        self.used = time.time()
        with self.lock:
            if not self.whisper:
                from faster_whisper import WhisperModel
                local = MODELS / f"whisper-{WHISPER_MODEL}"
                self.whisper = WhisperModel(str(local) if local.exists() else WHISPER_MODEL, device="cpu",
                                            compute_type="int8")  # the CPU: the graphics card is the AI model's
            model = self.whisper
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768
        names = people()
        segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True,
                                       initial_prompt=f"Hi. Names: {', '.join(names)}." if names else None)
        return " ".join(s.text.strip() for s in segments).strip()

    def unload_loop(self):
        while True:
            time.sleep(60)
            if self.whisper and time.time() - self.used > UNLOAD_MINUTES * 60:
                with self.lock:
                    self.whisper = None  # gives back about 500 MB
            if self.tts and not self.speaking.is_set() and time.time() - self.spoke > UNLOAD_MINUTES * 60:
                self.tts = None  # and the voice, about 400 MB

    # --- speaking ---

    def voice(self):
        """Piper: the fallback voice (and the self-check's, to make test speech)."""
        if not self.piper:
            from piper import PiperVoice
            self.piper = PiperVoice.load(str(MODELS / "piper" / f"{VOICE if '-' in VOICE else PIPER_VOICE}.onnx"))
        return self.piper

    def engine(self):
        """sentence -> (int16 audio, sample rate), in VOICE: Kokoro's natural voices, or Piper if Kokoro's model isn't
        downloaded (or VOICE names a Piper voice)."""
        self.spoke = time.time()
        if not self.tts:
            k = MODELS / "kokoro"
            if "-" not in VOICE and (k / "kokoro-v1.0.onnx").exists():
                from kokoro_onnx import Kokoro
                model = Kokoro(str(k / "kokoro-v1.0.onnx"), str(k / "voices-v1.0.bin"))
                lang = KOKORO_LANG.get(VOICE[0], "en-us")  # af_heart: American English, bf_emma: British, hf_: Hindi

                def tts(s):
                    audio, rate = model.create(s, voice=VOICE, speed=SPEED, lang=lang)
                    return (np.clip(audio, -1, 1) * 32767).astype(np.int16), rate
            else:
                piper = self.voice()

                def tts(s):
                    chunks = list(piper.synthesize(s))
                    return np.concatenate([c.audio_int16_array for c in chunks]), chunks[0].sample_rate
            self.tts = tts
        return self.tts

    def say(self, text, wait=False):
        """Reads text aloud, sentence by sentence (the first starts as soon as it's ready). Stops on shut_up()."""
        text = spoken(text)
        if not text:
            return
        self.shut_up()
        self.stop_speaking.clear()
        self.speaking.set()
        t = threading.Thread(target=self._say, args=(text,), daemon=True)
        t.start()
        if wait:
            t.join()

    def _say(self, text):
        import sounddevice as sd
        ready = queue.Queue()

        def make():  # the next sentence is made while this one plays: no pauses between them
            try:
                tts = self.engine()
                for sentence in sentences(text):
                    if self.stop_speaking.is_set():
                        break
                    ready.put(tts(sentence))
            except Exception as e:  # e.g. the voice model isn't downloaded
                print(f"voice: {e!r}", flush=True)
            finally:
                ready.put(None)
        threading.Thread(target=make, daemon=True).start()
        try:
            while (item := ready.get()) is not None:
                if self.stop_speaking.is_set():
                    return
                sd.play(*item)
                while sd.get_stream().active:
                    if self.stop_speaking.wait(0.05):
                        sd.stop()
                        return
        finally:
            time.sleep(0.3)  # the room's echo dies down before the wake word listens again
            self.speaking.clear()

    def shut_up(self):
        if self.speaking.is_set():
            self.stop_speaking.set()
            for _ in range(40):
                if not self.speaking.is_set():
                    break
                time.sleep(0.05)

    def chime(self):
        """A short soft "I'm listening" sound."""
        import sounddevice as sd
        t = np.arange(int(0.12 * 22050)) / 22050
        tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)]) * 0.15
        sd.play((tone * np.hanning(len(tone)) * 32767).astype(np.int16), 22050)
