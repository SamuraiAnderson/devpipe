import argparse
import logging
import os

from dotenv import load_dotenv

load_dotenv()

parser = argparse.ArgumentParser()
parser.add_argument("--debug", action="store_true")
args, _ = parser.parse_known_args()

DEBUG = args.debug

TMP_PATH = os.getenv("TMP_PATH", "")

TEST_DATA_DIR = os.getenv("TEST_DATA_DIR", "")
NOISE_DIR = os.getenv("NOISE_DIR", "")
VOICE_DIR = os.getenv("VOICE_DIR", "")
MIXED_DIR = os.getenv("MIXED_DIR", "")
RTC_DIR = os.getenv("RTC_DIR", "")
WORKPLACE = os.getenv("WORKPLACE", "")

WEBRTC_PATH = os.getenv("WEBRTC_PATH", "")
PLAY_PATH = os.getenv("PLAY_PATH", "")
BIN_PATH = os.getenv("BIN_PATH", "")

ANDROID_LIB_PATH = os.getenv("ANDROID_LIB_PATH", "/oem/lib")
ANDROID_BIN_PATH = os.getenv("ANDROID_BIN_PATH", "/data/bin")
PCM_DIR = os.getenv("PCM_DIR", "/data/pcm")

ANDROID_HOST = os.getenv("ANDROID_HOST", "")
LINUX_HOST = os.getenv("LINUX_HOST", "")
LINUX_USER = os.getenv("LINUX_USER", "")

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(levelname)s: %(message)s"
)
