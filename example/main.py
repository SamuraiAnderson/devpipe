
from pydub import AudioSegment

import example.config as config

import itertools
import logging
import os
import csv
import sys
import time

def get_file(folder_path):
    file_list = []
    for file in os.listdir(folder_path):
        file_path = os.path.join(folder_path, file)
        if os.path.isfile(file_path):
            file_list.append(file_path)
    return file_list

def get_mix(file1, offset1, file2, offset2, mix_file):
    sound1 = AudioSegment.from_file(file1, format="raw", frame_rate=16000, 
                            channels=1, sample_width=2)
    sound2 = AudioSegment.from_file(file2, format="raw", frame_rate=16000, 
                            channels=1, sample_width=2)
    sound1 = sound1- offset1
    sound2 = sound2- offset2
    mixed_sound = sound1.overlay(sound2)
    mixed_sound.export(mix_file, format="raw")

def get_mixed_name(voice, offset1, noise, offset2):
    return f"{os.path.basename(voice)}_{str(offset1)}_{os.path.basename(noise)}_{str(offset2)}.pcm"

def get_mixed_music_list():

    noise_dir = config.NOISE_DIR
    voice_dir = config.VOICE_DIR
    mixed_dir = config.MIXED_DIR

    voice_li = get_file(voice_dir)
    noise_li = get_file(noise_dir)
    mix_product = list(itertools.product(voice_li, noise_li))
    for voice, noise in mix_product:
        for offset in range(0, 18, 6):
            get_mix(voice, 0, noise, offset, 
                    os.path.join(mixed_dir, get_mixed_name(voice, 0, noise, offset))
            )

def src_to_mix():
    noise_dir = config.NOISE_DIR
    voice_dir = config.VOICE_DIR
    mixed_dir = config.MIXED_DIR

    voice_li = get_file(voice_dir)
    noise_li = get_file(noise_dir)
    out_li = []
    
    mix_product = list(itertools.product(voice_li, noise_li))
    for voice, noise in mix_product:
        for offset in range(0, 18, 6):
            mixed_file = os.path.join(mixed_dir, get_mixed_name(voice, 0, noise, offset))
            out_li.append((voice, mixed_file))
    return out_li

def write_to_csv(data, file_path):
    with open(file_path, 'w+', newline='') as csv_file:
        writer = csv.writer(csv_file)
        for row in data:
            writer.writerow(row)

def count_mse(pcm_file1, pcm_file2):
    import numpy as np
    from scipy.fft import fft
    from scipy.spatial.distance import cosine

    # 读取PCM文件并转换为AudioSegment对象
    pcm_array1 = np.fromfile(pcm_file1, dtype=np.int16)
    pcm_array2 = np.fromfile(pcm_file2, dtype=np.int16)

    # 计算均方误差
    if len(pcm_array1) != len(pcm_array2):
        if len(pcm_array1) > len(pcm_array2):
            pcm_array1 = pcm_array1[:len(pcm_array2)]
        else:
            pcm_array2 = pcm_array2[:len(pcm_array1)]
        # raise ValueError("PCM files should have the same length.")
    squared_diff = np.square(pcm_array1 - pcm_array2)
    mse = np.mean(squared_diff)

    # 执行FFT变换获取频谱
    spectrum1 = np.abs(fft(pcm_array1))
    spectrum2 = np.abs(fft(pcm_array2))

    # 计算余弦相似度
    similarity = 1 - cosine(spectrum1, spectrum2)

    return mse, similarity


def make_test():
    from src.make_style import make_style_decorate
    from src.file import UFile
    from src.adb_connector import AdbCnet
    from src.linux_controller import Linux
    from src.localhost import LocalHost
    from src.BaseControl import RemoteError

    try:
        @make_style_decorate
        def cp(target:UFile, src:UFile):
            import shutil
            nonlocal local
            print(f"{src} to {target}")
            if isinstance(target.RemoteUser, LocalHost) and isinstance(src.RemoteUser, LocalHost):
                shutil.copyfile(src.get_abs_path(), target.get_abs_path())
            elif not isinstance(src.RemoteUser, LocalHost):
                path = src.get_abs_path()
                if isinstance(target.RemoteUser, LocalHost):
                    temp_path = target.get_abs_path()
                    src.RemoteUser.pull(path, temp_path) # TODO:需要清除
                else:
                    temp_path = os.path.join(config.TMP_PATH, os.path.basename(path))
                    src.RemoteUser.pull(path, temp_path)
                    cp(target, UFile(local, temp_path))
            elif not isinstance(target.RemoteUser, LocalHost):
                path = src.get_abs_path()
                target.RemoteUser.push(path, target.get_abs_path())

        android = AdbCnet(config.ANDROID_HOST)
        linux = Linux(config.LINUX_HOST, config.LINUX_USER)
        local = LocalHost()
        local.pwd = config.TEST_DATA_DIR

        webrtc_path = config.WEBRTC_PATH
        play_path = config.PLAY_PATH
        android_lib_path = config.ANDROID_LIB_PATH
        bin_path = config.BIN_PATH
        android_bin_path = config.ANDROID_BIN_PATH

        linux.pwd = webrtc_path
        linux.shell("make", "-f", "Makefile.rv1106l")
        webrtc_core = UFile(android, rf"{android_lib_path}/libwebrtc_core.so")
        cp(UFile(android, android_lib_path), webrtc_core)

        linux.pwd = play_path
        linux.shell("./build_cmake.sh")

        @make_style_decorate
        def cp_exe(target:UFile, src:UFile):
            ''' TODO:target may be dir '''
            cp(target, src)
            target.RemoteUser.shell(f"chmod 755 {target.path}")

        linux.pwd = bin_path
        webrtc = UFile(android, f"{android_bin_path}/voice_play")
        cp_exe(webrtc, UFile(linux, "voice_play"))

        pcm_dir = config.PCM_DIR
        rtc_dir = config.RTC_DIR
        src_to_mix_li = src_to_mix()

        @make_style_decorate
        def webrtc_run(rtced_file:UFile, src_file:UFile, rtc_exe:UFile):
            nonlocal android
            file = UFile(android, f"{config.PCM_DIR}/temp_src.pcm")
            rtc_file = UFile(android, f"{config.PCM_DIR}/temp_rtc.pcm")
            cp(file, src_file, target_count=-1)
            rtc_exe.RemoteUser.shell(rtc_exe.path, file.path, rtc_file.path)
            cp(rtced_file, rtc_file, target_count=-1)

        local.pwd = config.RTC_DIR
        similar_data = []
        for src, mixed in src_to_mix_li:
            name = os.path.basename(mixed)
            rtc_name = rf"{name}_rtc.pcm"
            local_rtc_file = UFile(local, rtc_name)
            webrtc_run(local_rtc_file, UFile(local, mixed), webrtc, webrtc_core)
            similar_data.append([src, local_rtc_file]+list(count_mse(src, local_rtc_file.path)))

        write_to_csv(similar_data, os.path.join(config.TEST_DATA_DIR, f"result_{time.time()}.csv"))
    except RemoteError as e:
        logging.error("Test failed: %s", e, exc_info=config.DEBUG)
        sys.exit(1)
